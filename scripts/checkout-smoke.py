#!/usr/bin/env python3
"""Check one Online Boutique demo checkout with a fresh cookie session.

Uses only the checkout form's sample defaults; no personal/payment inputs are
accepted. Contract: GoogleCloudPlatform/microservices-demo v0.2.3 frontend.
Exit 0 means the observed outcome matches --expect; all other outcomes exit 1.
"""

import argparse
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.cookiejar import CookieJar
import ipaddress
import json
from pathlib import Path
import re
import sys
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.text_parts = []
        self.checkout = False
        self.fields = {}
        self.select_name = None
        self.options = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href", "").startswith("/product/"):
            self.links.append(attrs["href"])
        if tag == "form":
            self.checkout = attrs.get("action") == "/cart/checkout"
        if not self.checkout:
            return
        if tag == "input" and attrs.get("name") and "disabled" not in attrs:
            self.fields[attrs["name"]] = attrs.get("value", "")
        elif tag == "select":
            self.select_name = attrs.get("name")
            self.options = []
        elif tag == "option" and self.select_name:
            self.options.append((attrs.get("value", ""), "selected" in attrs))

    def handle_endtag(self, tag):
        if tag == "select" and self.checkout and self.select_name:
            if self.options:
                self.fields[self.select_name] = next(
                    (value for value, chosen in self.options if chosen), self.options[0][0]
                )
            self.select_name = None
            self.options = []
        elif tag == "form":
            self.checkout = False

    def handle_data(self, data):
        self.text_parts.append(data)

    @property
    def text(self):
        return " ".join(" ".join(self.text_parts).split())


class RecordedRedirects(HTTPRedirectHandler):
    def __init__(self):
        self.steps = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.steps.append({
            "at_utc": utc_now(), "method": req.get_method(), "url": req.full_url,
            "http_status": code, "location": newurl,
        })
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def assert_cart(page, product_id):
    if "SKU: #" + product_id not in page.text or "1 item in your cart" not in page.text:
        raise RuntimeError("Cart does not contain exactly one selected product")


def checkout_failure_markers(page):
    text = page.text.lower()
    markers = [marker for marker in (
        "no such host", "lookup checkoutservice", "name resolver error",
        "name resolution", "dns resolution", "dial tcp", "rpc error",
    ) if marker in text]
    if "checkoutservice" not in text or not markers:
        raise RuntimeError("HTTP 500 has no checkoutservice-related connection error")
    return ["checkoutservice"] + markers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="Private lab NodePort or loopback URL")
    parser.add_argument("--expect", choices=("success", "failure"), default="success")
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=45, help="Seconds per HTTP request")
    args = parser.parse_args()
    parsed = urlparse(args.base_url)
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        parser.error("Use an http(s) lab URL without credentials")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except (ValueError, TypeError):
        parser.error("Use the lab's private or loopback IP address")
    if not (address.is_private or address.is_loopback):
        parser.error("This smoke check only targets private or loopback lab addresses")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        parser.error("Use a base URL without a path, query or fragment")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    base_url = args.base_url.rstrip("/")
    evidence_root = args.evidence_dir.resolve()
    evidence_root.mkdir(parents=True, exist_ok=True)
    attempt_name = "attempt-{}-{}".format(
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"), uuid.uuid4().hex[:6]
    )
    evidence_dir = evidence_root / attempt_name
    evidence_dir.mkdir()
    jar = CookieJar()
    redirects = RecordedRedirects()
    opener = build_opener(HTTPCookieProcessor(jar), redirects)
    started = time.monotonic()
    result = {
        "base_url": base_url, "status": "FAIL", "expected_outcome": args.expect,
        "evidence_dir": str(evidence_dir), "started_at_utc": utc_now(), "steps": [],
    }

    def request(path, evidence_name, fields=None, expected_status=200):
        data = urlencode(fields).encode("utf-8") if fields is not None else None
        req = Request(base_url + path, data=data,
                      headers={"User-Agent": "project01-checkout-smoke/1.0"})
        step = {"method": req.get_method(), "path": path, "started_at_utc": utc_now(),
                "http_status": None, "html_file": None}
        result["steps"].append(step)
        redirects.steps = []
        try:
            try:
                response = opener.open(req, timeout=args.timeout)
            except HTTPError as error:
                response = error
            with response:
                step["http_status"] = response.code
                step["response_url"] = response.geturl()
                body = response.read().decode("utf-8", errors="replace")
            (evidence_dir / evidence_name).write_text(body, encoding="utf-8")
            step["html_file"] = evidence_name
            if step["http_status"] != expected_status:
                raise RuntimeError("{} returned HTTP {}, expected {}; see {}".format(
                    path, step["http_status"], expected_status, evidence_name))
            page = Page()
            page.feed(body)
            return page
        except (RuntimeError, URLError, OSError, TimeoutError) as error:
            step["error"] = str(error)
            raise
        finally:
            step["redirects"] = redirects.steps[:]
            step["finished_at_utc"] = utc_now()

    try:
        home = request("/", "01-home.html")
        if not home.links:
            raise RuntimeError("Homepage has no product links")
        if not any(cookie.name == "shop_session-id" for cookie in jar):
            raise RuntimeError("Frontend did not set its session cookie")
        product_path = home.links[0]
        product_id = product_path.removeprefix("/product/")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", product_id):
            raise RuntimeError("Unexpected product ID on homepage")
        result["product_id"] = product_id
        request(product_path, "02-product.html")
        request("/cart", "03-add-to-cart.html", {"product_id": product_id, "quantity": "1"})
        cart = request("/cart", "04-cart.html")
        assert_cart(cart, product_id)
        cart = request("/cart", "05-cart-reloaded.html")
        assert_cart(cart, product_id)
        result["cart_persisted_on_reload"] = True
        fields = (
            "email", "street_address", "zip_code", "city", "state", "country",
            "credit_card_number", "credit_card_expiration_month",
            "credit_card_expiration_year", "credit_card_cvv",
        )
        if any(not cart.fields.get(name) for name in fields):
            raise RuntimeError("Checkout form is missing expected demo fields/defaults")
        checkout = request("/cart/checkout", "06-checkout.html",
                           {name: cart.fields[name] for name in fields},
                           expected_status=200 if args.expect == "success" else 500)
        if args.expect == "success":
            if "Your order is complete!" not in checkout.text:
                raise RuntimeError("Checkout did not render order-complete confirmation")
            order_match = re.search(
                r"Order Confirmation ID\s+([A-Za-z0-9-]+)\s+Shipping Tracking ID", checkout.text)
            if not order_match:
                raise RuntimeError("Checkout confirmation has no order ID")
            result["order_id"] = order_match.group(1)
        else:
            result["failure_markers"] = checkout_failure_markers(checkout)
        after = request("/cart", "07-cart-after-checkout.html")
        if args.expect == "success":
            if "Your shopping cart is empty!" not in after.text:
                raise RuntimeError("Successful checkout did not clear the cart")
            result["cart_cleared"] = True
        else:
            assert_cart(after, product_id)
            result["cart_preserved_after_failure"] = True
        result["status"] = "PASS"
    except (RuntimeError, URLError, OSError, TimeoutError) as error:
        result["error"] = str(error)
    result["finished_at_utc"] = utc_now()
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    output = json.dumps(result, indent=2)
    (evidence_dir / "smoke-result.json").write_text(output + "\n", encoding="utf-8")
    (evidence_root / "latest-result.json").write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
