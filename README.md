# Untung

https://packages.blankonlinux.id/

## Usage

```
python3 untung.py --repo=http://arsip.blankonlinux.id/sinambung/ --repo=http://arsip-dev.blankonlinux.id/sinambung/ --upstream-repo=https://kartolo.sby.datautama.net.id/debian/ --html=./ && open ./index.html
```

List the repos downstream first. Each `--repo` is treated as the upstream of
the one before it, so every repo also gets a "Diff against <repo>" tab for each
repo listed after it. `--upstream-repo` is shown by its dist name (e.g. "Sid"),
so it is not confused with a BlankOn repo such as arsip-upstream.

For example, with arsip, arsip-dev and arsip-upstream (in that order) and Sid
as `--upstream-repo`:

- arsip is diffed against arsip-dev, arsip-upstream and Sid
- arsip-dev is diffed against arsip-upstream and Sid
- arsip-upstream is diffed against Sid

A repo may name its suite after an `@`. Without one, every dist advertised
under `{repo}/dists/` is indexed and merged; the upstream dist defaults to
`sid`. `--upstream-dist=<dist>` is accepted as an alternative to `@<dist>` on
`--upstream-repo`.

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
