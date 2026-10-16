#!/usr/bin/env python3
"""Bounded demo purchases for Online Boutique v0.2.3; standard library only.

Every worker owns its cookie jar. Only the checkout form's built-in sample
inputs are submitted. The output contains timings and counts, not HTML or
checkout form values. Use only a private/loopback lab endpoint.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.cookiejar import CookieJar
import importlib.util
import ipaddress
import json
import math
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "checkout_smoke", Path(__file__).with_name("checkout-smoke.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_url(value):
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        raise argparse.ArgumentTypeError("Use an http(s) lab URL without credentials")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except (ValueError, TypeError) as error:
        raise argparse.ArgumentTypeError("Use a private or loopback IP address") from error
    if not (address.is_private or address.is_loopback):
        raise argparse.ArgumentTypeError("Only private/loopback lab addresses are accepted")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError("Use a base URL without a path/query/fragment")
    return value.rstrip("/")


def run_load(base_url, output_dir, duration=300, workers=2, cycle_interval=5,
             timeout=15):
    """Run at most one purchase per worker per cycle_interval seconds.

    A worker waits after a fast cycle and never catches up after a slow cycle.
    No new cycle starts after the deadline; an in-flight cycle is bounded by
    the same deadline plus one request timeout.
    """
    smoke = load_smoke_module()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    started_at = utc_now()
    started = time.monotonic()
    deadline = started + duration
    lock = threading.Lock()
    results = []
    checkout_fields = (
        "email", "street_address", "zip_code", "city", "state", "country",
        "credit_card_number", "credit_card_expiration_month",
        "credit_card_expiration_year", "credit_card_cvv",
    )

    with (output_dir / "cycles.jsonl").open("w", encoding="utf-8") as output:
        def worker(worker_id):
            jar = CookieJar()
            opener = build_opener(HTTPCookieProcessor(jar))
            sequence = 0
            # Spread the initial requests across the configured cycle interval.
            time.sleep(min(worker_id * cycle_interval / workers, duration))
            while time.monotonic() < deadline:
                sequence += 1
                cycle_started = time.monotonic()
                record = {"worker": worker_id, "cycle": sequence,
                          "started_at_utc": utc_now(), "requests": [], "status": "FAIL"}

                def request(path, fields=None):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Load deadline reached during a purchase")
                    req = Request(base_url + path,
                                  data=urlencode(fields).encode() if fields is not None else None,
                                  headers={"User-Agent": "project01-bounded-load/1.0"})
                    step_started = time.monotonic()
                    step = {"method": req.get_method(), "path": path, "http_status": None}
                    record["requests"].append(step)
                    try:
                        try:
                            response = opener.open(req, timeout=min(timeout, remaining))
                        except HTTPError as error:
                            response = error
                        with response:
                            step["http_status"] = response.code
                            body = response.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
                        if step["http_status"] != 200:
                            raise RuntimeError("{} returned HTTP {}".format(path, step["http_status"]))
                        page = smoke.Page()
                        page.feed(body)
                        return page
                    finally:
                        step["elapsed_seconds"] = round(time.monotonic() - step_started, 4)

                try:
                    home = request("/")
                    if not home.links or not any(c.name == "shop_session-id" for c in jar):
                        raise RuntimeError("Missing product links or frontend session cookie")
                    product_path = home.links[(worker_id + sequence - 1) % len(home.links)]
                    product_id = product_path.removeprefix("/product/")
                    if not re.fullmatch(r"[A-Za-z0-9_-]+", product_id):
                        raise RuntimeError("Unexpected product ID")
                    request(product_path)
                    request("/cart", {"product_id": product_id, "quantity": "1"})
                    cart = request("/cart")
                    smoke.assert_cart(cart, product_id)
                    if any(not cart.fields.get(name) for name in checkout_fields):
                        raise RuntimeError("Missing demo checkout form defaults")
                    confirmation = request("/cart/checkout",
                                           {name: cart.fields[name] for name in checkout_fields})
                    if "Your order is complete!" not in confirmation.text:
                        raise RuntimeError("Missing order-complete confirmation")
                    empty = request("/cart")
                    if "Your shopping cart is empty!" not in empty.text:
                        raise RuntimeError("Checkout did not clear the cart")
                    record["status"] = "PASS"
                except Exception as error:
                    record["error"] = "{}: {}".format(type(error).__name__, error)
                    # A failed cycle can retain cart contents. Start a fresh
                    # session on the next attempt rather than adding to it.
                    jar.clear()
                record["finished_at_utc"] = utc_now()
                record["elapsed_seconds"] = round(time.monotonic() - cycle_started, 4)
                with lock:
                    results.append(record)
                    output.write(json.dumps(record, sort_keys=True) + "\n")
                    output.flush()
                pause = min(cycle_interval - (time.monotonic() - cycle_started),
                            deadline - time.monotonic())
                if pause > 0:
                    time.sleep(pause)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(worker, range(workers)))

    successes = sum(record["status"] == "PASS" for record in results)
    latencies = sorted(record["elapsed_seconds"] for record in results
                       if record["status"] == "PASS")
    elapsed = time.monotonic() - started
    summary = {
        "status": "PASS" if results and successes == len(results) else "FAIL",
        "started_at_utc": started_at, "finished_at_utc": utc_now(),
        "base_url": base_url, "workers": workers, "duration_seconds": duration,
        "cycle_interval_seconds": cycle_interval, "request_timeout_seconds": timeout,
        "maximum_started_cycles_per_second": workers / cycle_interval,
        "actual_elapsed_seconds": round(elapsed, 3), "cycles": len(results),
        "successful_cycles": successes, "failed_cycles": len(results) - successes,
        "http_steps": sum(len(r["requests"]) for r in results),
        "successful_cycles_per_second": round(successes / elapsed, 4),
        "successful_cycle_p95_seconds": latencies[math.ceil(len(latencies) * .95) - 1]
        if latencies else None,
        "note": "HTTP steps count opener calls; automatic redirect hops are additional requests.",
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", type=validate_url)
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory; never overwrite a run")
    parser.add_argument("--duration", type=float, default=300)
    parser.add_argument("--workers", type=int, default=2, choices=range(1, 5))
    parser.add_argument("--cycle-interval", type=float, default=5)
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()
    if not 1 <= args.duration <= 1800 or args.cycle_interval < 2 or not 0 < args.timeout <= 60:
        parser.error("Use duration 1–1800 s, cycle interval >=2 s and timeout >0–60 s")
    summary = run_load(args.base_url, args.output_dir, args.duration, args.workers,
                       args.cycle_interval, args.timeout)
    print(json.dumps(summary, indent=2))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
