# Untung

https://packages.blankonlinux.id/

## Usage

```
python3 untung.py --repo=http://arsip.blankonlinux.id/sinambung/ --repo=http://arsip-dev.blankonlinux.id/sinambung/ --upstream-repo=https://kartolo.sby.datautama.net.id/debian/ --html=./ && python3 -m http.server -d ./
```

then open http://localhost:8000/.

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

`--html=<dir>` writes:

- `index.html`: the page itself, without any table data, so it opens at once;
- `data/`: one gzip-compressed JSON file per table, named after a hash of its
  content (`r0-arsip-pkg.3f9c1a2b7d4e.json.gz`). The page fetches a table's
  file the first time its tab is opened and unpacks it in the browser, so no
  server-side compression is needed. Unchanged tables keep their file name
  between runs, so browsers reuse the copy they already have;
- `index.html.gz`: a compressed copy of the page.

Publish the whole folder, `data/` included. Each run writes the data files
first and the page last, and keeps the files of the page it replaces for at
least an hour, so a page that is already open can still load its tabs. Runs
that overlap wait for each other.

Browsers cannot fetch the data files of a page opened straight from disk
(`file://`), so preview the report through a web server, such as
`python3 -m http.server -d <dir>`.

With nginx, `gzip_static` (module `ngx_http_gzip_static_module`, included in
Debian's nginx packages) lets browsers download the compressed `index.html.gz`
instead of `index.html`. It is optional: the table data is already compressed.

```
location / {
    gzip_static on;
}
```

## Tests

```
python3 -m unittest discover -s tests
```

One test needs `dpkg --compare-versions` and is skipped where `dpkg` is not
installed.
