# Vendored microservice chart

`microservice/` is an unchanged copy of `chart/microservice` from
[iankisali/microservice-deployment](https://github.com/iankisali/microservice-deployment/tree/8ed96c8d98625c73b67c7172b6703383ea9e46e9/chart/microservice),
pinned at commit `8ed96c8d98625c73b67c7172b6703383ea9e46e9` and imported on
2026-10-15 with `git -c core.autocrlf=false -c core.eol=lf archive`. All five files
match the upstream Git blobs.
[UPSTREAM.json](UPSTREAM.json) records the source and per-file SHA-256 checksums;
the root `.gitattributes` preserves LF line endings on Windows.

This import retains chart version `0.1.0`, all templates, and the generic defaults
(including `appName: servicename`). Deployment values and project overrides still
supply each service's name, image and configuration. In particular, checkout needs
the existing `appName: checkoutservice` override. The upstream Redis chart and
service-specific values are outside this import.

For future local behavior changes, increment `microservice/Chart.yaml`'s chart
version and document the changes. Keep the import checksums as the original
snapshot reference; update the provenance when deliberately importing a new
upstream revision. Metadata stays outside `microservice/` so it does not enter the
packaged chart.

The pinned upstream snapshot contains no `LICENSE` file. This import adds no
license and makes no assumption about one.
