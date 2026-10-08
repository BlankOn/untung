# Untung

https://packages.blankonlinux.id/

## Usage

```
python3 untung.py \
  --repo=http://arsip.blankonlinux.id/@verbeek \
  --repo=http://arsip-dev.blankonlinux.id/dev/@verbeek \
  --upstream-repo=https://kartolo.sby.datautama.net.id/debian/@sid \
  --html=./
```

A repo may name its suite after an `@`. Without one, every dist advertised
under `{repo}/dists/` is indexed and merged; the upstream dist defaults to
`sid`. `--upstream-dist=<dist>` is accepted as an alternative to `@<dist>` on
`--upstream-repo`.

The report renders two comparisons per repo, as tabs:

- **Live Build Comparison** — the packages listed in
  [blankon-live-build](https://github.com/BlankOn/blankon-live-build/tree/main/config/package-lists),
  compared against upstream.
- **Full Upstream Diff** — every package in the repo, showing only those whose
  version differs from upstream (behind, ahead, or not carried upstream).
  **Group by** collapses the list into expandable groups: by Debian source
  package (the `Source:` field, so one group is one upload to test), or by
  the first one or two dash-separated parts of the package name.

## Serving the report

`--html=<dir>` writes `index.html` and a gzip-compressed copy,
`index.html.gz`, about a sixth of its size. With nginx, let browsers download
the compressed copy by turning on `gzip_static` (module
`ngx_http_gzip_static_module`, included in Debian's nginx packages) where the
report is served:

```
location / {
    gzip_static on;
}
```

Without it, nginx keeps serving the uncompressed `index.html`.

## Tests

```
python3 -m unittest discover -s tests
```

One test needs `dpkg --compare-versions` and is skipped where `dpkg` is not
installed.
