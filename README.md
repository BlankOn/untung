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
