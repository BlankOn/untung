#!/usr/bin/env python3
import gzip
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from io import BytesIO
import urllib.request

LIVE_BUILD_REPO = "https://github.com/BlankOn/blankon-live-build.git"
LIVE_BUILD_PKG_DIR = "config/package-lists"
UPSTREAM_DEFAULT_DIST = "sid"


# ── helpers ───────────────────────────────────────────────────────────────────

def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as r:
        return r.read()


def fetch_text(url):
    return fetch_bytes(url).decode("utf-8")


def version_lt(v1, v2):
    """Return True if v1 < v2 using dpkg version comparison."""
    result = subprocess.run(
        ["dpkg", "--compare-versions", v1, "lt", v2],
        capture_output=True,
    )
    return result.returncode == 0


# ── package list ──────────────────────────────────────────────────────────────

def fetch_package_list(repo=LIVE_BUILD_REPO, pkg_dir=LIVE_BUILD_PKG_DIR):
    """
    Shallow-clone the live-build repo into a temp dir, read all package list
    files under pkg_dir, and return a deduplicated sorted list of package names.
    """
    tmpdir = tempfile.mkdtemp(prefix="untung-livebuild-")
    try:
        print(f"Cloning {repo} ...", file=sys.stderr)
        result = subprocess.run(
            ["git", "clone", "--depth=1", "--quiet", repo, tmpdir],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"Error: git clone failed:\n{result.stderr}", file=sys.stderr)
            sys.exit(1)

        lists_dir = os.path.join(tmpdir, pkg_dir)
        if not os.path.isdir(lists_dir):
            print(f"Error: {pkg_dir} not found in cloned repo.", file=sys.stderr)
            sys.exit(1)

        packages = set()
        for fname in sorted(os.listdir(lists_dir)):
            fpath = os.path.join(lists_dir, fname)
            if not os.path.isfile(fpath):
                continue
            print(f"  Reading {fname} ...", file=sys.stderr)
            with open(fpath) as f:
                for line in f:
                    pkg = line.strip()
                    if pkg and not pkg.startswith("#"):
                        packages.add(pkg)

        print(f"  Loaded {len(packages)} unique packages.", file=sys.stderr)
        return sorted(packages)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── repo index ────────────────────────────────────────────────────────────────

def discover_dists(repo_url):
    """Parse HTML directory listing at {repo}/dists/ and return dist names."""
    url = repo_url.rstrip("/") + "/dists/"
    html = fetch_text(url)
    names = re.findall(r'href="([^"/][^"]*/)\"', html)
    return [n.rstrip("/") for n in names]


def fetch_release(repo_url, dist):
    """Return (codename, components list) from a Release file."""
    url = f"{repo_url.rstrip('/')}/dists/{dist}/Release"
    try:
        text = fetch_text(url)
    except Exception:
        return dist, []
    components = []
    for line in text.splitlines():
        if line.startswith("Components:"):
            components = line.split(":", 1)[1].strip().split()
            break
    return dist, components


def fetch_packages(repo_url, dist, component, arch="amd64"):
    """
    Fetch and parse Packages.gz; return dict of binary package ->
    {version, url, source}. The source is the package's Source: field without
    any "(version)" suffix, or the package's own name when the field is absent.
    """
    base = repo_url.rstrip("/")
    url = f"{base}/dists/{dist}/{component}/binary-{arch}/Packages.gz"
    try:
        data = fetch_bytes(url)
    except Exception:
        return {}

    try:
        text = gzip.decompress(data).decode("utf-8")
    except Exception:
        return {}

    packages = {}
    current_pkg = None
    current_ver = None
    current_filename = None
    current_source = None

    for line in text.splitlines():
        if line.startswith("Package:"):
            current_pkg = line.split(":", 1)[1].strip()
            current_ver = None
            current_filename = None
            current_source = None
        elif line.startswith("Source:"):
            current_source = line.split(":", 1)[1].split()[0]
        elif line.startswith("Version:"):
            current_ver = line.split(":", 1)[1].strip()
        elif line.startswith("Filename:"):
            current_filename = line.split(":", 1)[1].strip()
            if current_pkg and current_ver and current_filename:
                existing = packages.get(current_pkg)
                if existing is None or version_lt(existing["version"], current_ver):
                    packages[current_pkg] = {
                        "version": current_ver,
                        "url": f"{base}/{current_filename}",
                        "source": current_source or current_pkg,
                    }
    return packages


def build_package_index(repo_url, arch="amd64", dist=None):
    """
    Return a unified binary package -> {version, url} map for the repo.

    With a dist (suite) given, only that dist is indexed. Without one, every
    dist advertised under {repo}/dists/ is walked and merged.
    """
    if dist:
        dists = [dist]
        print(f"Indexing {repo_url} dist {dist} ...", file=sys.stderr)
    else:
        print(f"Discovering dists at {repo_url} ...", file=sys.stderr)
        try:
            dists = discover_dists(repo_url)
        except Exception as exc:
            print(f"  Warning: failed to reach {repo_url}: {exc}", file=sys.stderr)
            return {}
        if not dists:
            print(f"  Warning: no dists found at {repo_url}, skipping.", file=sys.stderr)
            return {}
        print(f"  Found dists: {', '.join(dists)}", file=sys.stderr)

    index = {}
    for d in dists:
        _, components = fetch_release(repo_url, d)
        if not components:
            print(f"  Warning: {d} release not found or has no components.", file=sys.stderr)
            continue
        for component in components:
            print(f"  Fetching {d}/{component}/binary-{arch}/Packages.gz ...", file=sys.stderr)
            pkgs = fetch_packages(repo_url, d, component, arch)
            for pkg, info in pkgs.items():
                existing = index.get(pkg)
                if existing is None or version_lt(existing["version"], info["version"]):
                    index[pkg] = info

    print(f"  Indexed {len(index)} binary packages.", file=sys.stderr)
    return index


def build_upstream_index(repo_url, arch="amd64", dist=UPSTREAM_DEFAULT_DIST):
    """Fetch binary package versions from one dist (suite) of an upstream repo."""
    print(f"Fetching {dist} index from upstream {repo_url} ...", file=sys.stderr)
    try:
        _, components = fetch_release(repo_url, dist)
    except Exception as exc:
        print(f"  Warning: failed to reach upstream {repo_url}: {exc}", file=sys.stderr)
        return {}
    if not components:
        print(f"  Warning: {dist} release not found or has no components.", file=sys.stderr)
        return {}

    index = {}
    for component in components:
        print(f"  Fetching {dist}/{component}/binary-{arch}/Packages.gz ...", file=sys.stderr)
        pkgs = fetch_packages(repo_url, dist, component, arch)
        for pkg, info in pkgs.items():
            existing = index.get(pkg)
            if existing is None or version_lt(existing["version"], info["version"]):
                index[pkg] = info

    print(f"  Indexed {len(index)} binary packages from {dist}.", file=sys.stderr)
    return index


# ── compare ───────────────────────────────────────────────────────────────────

def compare_versions(packages, repo_index, upstream_index):
    """
    For each package in the list, compare repo version vs upstream version.
    Returns a list of dicts for packages that are behind upstream.
    """
    results = []
    for pkg in packages:
        repo_info = repo_index.get(pkg)
        upstream_info = upstream_index.get(pkg)

        repo_ver = repo_info["version"] if repo_info else None
        upstream_ver = upstream_info["version"] if upstream_info else None

        if repo_ver is None:
            results.append({
                "package": pkg,
                "repo_version": None,
                "repo_url": None,
                "upstream_version": upstream_ver,
                "status": "not_in_repo",
            })
            continue

        if upstream_ver is None:
            results.append({
                "package": pkg,
                "repo_version": repo_ver,
                "repo_url": repo_info["url"],
                "upstream_version": None,
                "status": "not_in_upstream",
            })
            continue

        if version_lt(repo_ver, upstream_ver):
            results.append({
                "package": pkg,
                "repo_version": repo_ver,
                "repo_url": repo_info["url"],
                "upstream_version": upstream_ver,
                "status": "behind",
            })
        else:
            results.append({
                "package": pkg,
                "repo_version": repo_ver,
                "repo_url": repo_info["url"],
                "upstream_version": upstream_ver,
                "status": "up_to_date",
            })

    return results


def compare_repo_diff(repo_index, upstream_index):
    """
    Compare every binary package in the repo against upstream and return only
    the packages whose version differs (or that upstream does not carry).

    Unlike compare_versions(), this is not scoped to the blankon-live-build
    package list -- it walks the whole repo index. Packages that upstream has
    but the repo does not are left out: the repo, not upstream, is the subject.
    """
    results = []
    for pkg in sorted(repo_index):
        repo_ver = repo_index[pkg]["version"]
        source = repo_index[pkg].get("source", pkg)
        upstream_info = upstream_index.get(pkg)

        if upstream_info is None:
            results.append({
                "package": pkg,
                "repo_version": repo_ver,
                "repo_url": repo_index[pkg]["url"],
                "upstream_version": None,
                "status": "not_in_upstream",
                "source": source,
            })
            continue

        upstream_ver = upstream_info["version"]
        if repo_ver == upstream_ver:
            continue

        results.append({
            "package": pkg,
            "repo_version": repo_ver,
            "repo_url": repo_index[pkg]["url"],
            "upstream_version": upstream_ver,
            "status": "behind" if version_lt(repo_ver, upstream_ver) else "ahead",
            "source": source,
        })

    return results


# ── html report ───────────────────────────────────────────────────────────────

def _repo_label(url, dist=None):
    """Display label for a repo: "hostname / dist" (or just the hostname)."""
    from urllib.parse import urlparse
    host = urlparse(url).hostname or url
    return f"{host} / {dist}" if dist else host


def write_html_report(repo_data_list, html_dir, upstream_url, upstream_index=None,
                      upstream_dist=UPSTREAM_DEFAULT_DIST):
    """
    repo_data_list: list of {"url": str, "index": dict, "results": list}
    """
    import html as _html
    import json as _json

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def e(s):
        return _html.escape(str(s)) if s is not None else ""

    def make_cmp_rows(results):
        ordered = (
            [r for r in results if r["status"] == "behind"] +
            [r for r in results if r["status"] == "not_in_repo"] +
            [r for r in results if r["status"] == "not_in_upstream"] +
            [r for r in results if r["status"] == "up_to_date"]
        )
        return [
            {
                "n": r["package"],
                "rv": r["repo_version"] or "",
                "uv": r["upstream_version"] or "",
                "s": r["status"],
            }
            for r in ordered
        ]

    def make_diff_rows(results):
        ordered = (
            [r for r in results if r["status"] == "behind"] +
            [r for r in results if r["status"] == "ahead"] +
            [r for r in results if r["status"] == "not_in_upstream"]
        )
        rows = []
        for r in ordered:
            row = {
                "n": r["package"],
                "rv": r["repo_version"] or "",
                "uv": r["upstream_version"] or "",
                "s": r["status"],
            }
            # Only name the source when it differs; the page falls back to "n".
            if r.get("source", r["package"]) != r["package"]:
                row["src"] = r["source"]
            rows.append(row)
        return rows

    # Build per-repo JS data blobs
    repos_js_entries = []
    for rd in repo_data_list:
        pkg_data = _json.dumps(
            [{"n": k, "v": info["version"], "u": info["url"]}
             for k, info in sorted(rd["index"].items())]
        )
        cmp_data = _json.dumps(make_cmp_rows(rd["results"]))
        diff_rows = make_diff_rows(rd.get("diff", []))
        diff_data = _json.dumps(diff_rows)
        label = e(_repo_label(rd["url"], rd.get("dist")))
        url = e(rd["url"])
        count_behind = sum(1 for r in rd["results"] if r["status"] == "behind")
        summary = (
            f"{count_behind} package(s) behind upstream."
            if count_behind
            else "All packages are up to date with upstream."
        )
        summary_color = "#c0392b" if count_behind else "#27ae60"

        diff_counts = {
            k: sum(1 for r in diff_rows if r["s"] == k)
            for k in ("behind", "ahead", "not_in_upstream")
        }
        if diff_rows:
            diff_summary = (
                f"{len(diff_rows)} of {len(rd['index'])} repo package(s) differ from upstream: "
                f"{diff_counts['behind']} behind, {diff_counts['ahead']} ahead, "
                f"{diff_counts['not_in_upstream']} not in upstream."
            )
            diff_summary_color = "#c0392b" if diff_counts["behind"] else "#e67e22"
        else:
            diff_summary = "Every repo package matches the upstream version."
            diff_summary_color = "#27ae60"

        repos_js_entries.append({
            "label": label,
            "url": url,
            "pkg_data": pkg_data,
            "cmp_data": cmp_data,
            "diff_data": diff_data,
            "summary": e(summary),
            "summary_color": summary_color,
            "diff_summary": e(diff_summary),
            "diff_summary_color": diff_summary_color,
        })

    # Build upstream JS data
    upstream_pkg_data = "[]"
    if upstream_index:
        upstream_pkg_data = _json.dumps(
            [{"n": k, "v": info["version"], "u": info["url"]}
             for k, info in sorted(upstream_index.items())]
        )

    # Generate repo meta line
    repo_links = " &nbsp;|&nbsp; ".join(
        f'<a href="{r["url"]}">{r["label"]}</a>'
        for r in repos_js_entries
    )

    # Generate top-level repo tab buttons (repos + upstream)
    repo_tab_btns = "\n    ".join(
        f'<button class="tab-btn repo-tab-btn{" active" if i == 0 else ""}" '
        f'onclick="switchRepoTab({i}, this)">{r["label"]}</button>'
        for i, r in enumerate(repos_js_entries)
    )
    repo_tab_btns += (
        '\n    <button class="tab-btn repo-tab-btn" '
        f'onclick="switchRepoTab(\'upstream\', this)">Upstream / {e(upstream_dist)}</button>'
    )

    # Generate repo panels (each with sub-tabs)
    repo_panels_html = ""
    for i, r in enumerate(repos_js_entries):
        active_panel = "active" if i == 0 else ""
        repo_panels_html += f"""
  <div id="repo-{i}" class="repo-panel {active_panel}">
    <div class="tabs sub-tabs" style="margin-top:1rem">
      <button class="tab-btn sub-tab-btn active" onclick="switchSubTab('r{i}-pkg-list', this, {i})">Package List</button>
      <button class="tab-btn sub-tab-btn" onclick="switchSubTab('r{i}-upstream-cmp', this, {i})">Live Build Comparison</button>
      <button class="tab-btn sub-tab-btn" onclick="switchSubTab('r{i}-upstream-diff', this, {i})">Full Upstream Diff</button>
    </div>

    <div id="r{i}-pkg-list" class="sub-panel active">
      <div class="toolbar">
        <input class="search-box" type="search" id="r{i}-pkg-search"
               placeholder="Search packages..." oninput="TABLES[{i}].pkg.search(this.value)">
        <span class="row-count" id="r{i}-pkg-count"></span>
      </div>
      <div class="pagination" id="r{i}-pkg-pages" style="margin-bottom:0.6rem"></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Package</th><th>Version</th></tr></thead>
          <tbody id="r{i}-pkg-tbody"><tr><td colspan="2" style="color:#999;font-style:italic">Loading...</td></tr></tbody>
        </table>
      </div>
      <div class="pagination" id="r{i}-pkg-pages-bottom" style="margin-top:0.6rem"></div>
    </div>

    <div id="r{i}-upstream-cmp" class="sub-panel">
      <div class="summary" style="color:{r['summary_color']};font-weight:bold;margin:0.8rem 0">{r['summary']}</div>
      <div class="toolbar">
        <input class="search-box" type="search" id="r{i}-cmp-search"
               placeholder="Search packages..." oninput="TABLES[{i}].cmp.search(this.value)">
        <span class="row-count" id="r{i}-cmp-count"></span>
        <span class="row-count">from <a href="https://github.com/BlankOn/blankon-live-build/tree/main/config/package-lists">blankon-live-build</a></span>
      </div>
      <div class="pagination" id="r{i}-cmp-pages" style="margin-bottom:0.6rem"></div>
      <div class="table-wrap">
        <table class="cmp-table">
          <thead><tr><th>Package</th><th>Repo version</th><th>Upstream version ({e(upstream_dist)})</th><th>Status</th></tr></thead>
          <tbody id="r{i}-cmp-tbody"><tr><td colspan="4" style="color:#999;font-style:italic">Loading...</td></tr></tbody>
        </table>
      </div>
      <div class="pagination" id="r{i}-cmp-pages-bottom" style="margin-top:0.6rem"></div>
    </div>

    <div id="r{i}-upstream-diff" class="sub-panel">
      <div class="summary" style="color:{r['diff_summary_color']};font-weight:bold;margin:0.8rem 0">{r['diff_summary']}</div>
      <div class="toolbar">
        <input class="search-box" type="search" id="r{i}-diff-search"
               placeholder="Search packages..." oninput="TABLES[{i}].diff.search(this.value)">
        <label class="group-by">Group by
          <select class="group-select" id="r{i}-diff-group" autocomplete="off" onchange="TABLES[{i}].diff.groupBy(this.value)">
            <option value="none">None</option>
            <option value="source">Source package</option>
            <option value="prefix1">Name prefix (1 level)</option>
            <option value="prefix2">Name prefix (2 levels)</option>
          </select>
        </label>
        <span class="group-tools" id="r{i}-diff-group-tools" hidden>
          <button class="pg-btn" type="button" onclick="TABLES[{i}].diff.expandPage()">Expand page</button>
          <button class="pg-btn" type="button" onclick="TABLES[{i}].diff.collapseAll()">Collapse all</button>
        </span>
        <span class="row-count" id="r{i}-diff-count"></span>
        <span class="row-count">every repo package whose version differs from upstream</span>
      </div>
      <div class="pagination" id="r{i}-diff-pages" style="margin-bottom:0.6rem"></div>
      <div class="table-wrap">
        <table class="cmp-table">
          <thead><tr><th>Package</th><th>Repo version</th><th>Upstream version ({e(upstream_dist)})</th><th>Status</th></tr></thead>
          <tbody id="r{i}-diff-tbody"><tr><td colspan="4" style="color:#999;font-style:italic">Loading...</td></tr></tbody>
        </table>
      </div>
      <div class="pagination" id="r{i}-diff-pages-bottom" style="margin-top:0.6rem"></div>
    </div>
  </div>
"""

    # Generate upstream panel HTML
    upstream_label = e(_repo_label(upstream_url, upstream_dist))
    repo_panels_html += f"""
  <div id="repo-upstream" class="repo-panel">
    <div class="tabs sub-tabs" style="margin-top:1rem">
      <button class="tab-btn sub-tab-btn active" onclick="switchSubTab('upstream-pkg-list', this, 'upstream')">Package List</button>
    </div>

    <div id="upstream-pkg-list" class="sub-panel active">
      <div class="toolbar">
        <input class="search-box" type="search" id="upstream-pkg-search"
               placeholder="Search packages..." oninput="UPSTREAM_TABLE.search(this.value)">
        <span class="row-count" id="upstream-pkg-count"></span>
      </div>
      <div class="pagination" id="upstream-pkg-pages" style="margin-bottom:0.6rem"></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Package</th><th>Version</th></tr></thead>
          <tbody id="upstream-pkg-tbody"><tr><td colspan="2" style="color:#999;font-style:italic">Loading...</td></tr></tbody>
        </table>
      </div>
      <div class="pagination" id="upstream-pkg-pages-bottom" style="margin-top:0.6rem"></div>
    </div>
  </div>
"""

    # Small external-link glyph, matching the one on blankonlinux.id.
    EXT_ICON = (
        '<svg class="nav-ext" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<path d="M15 3h6v6"/><path d="M10 14 21 3"/>'
        '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>'
    )

    # Generate JS data arrays
    all_pkg_data = "[" + ",\n".join(r["pkg_data"] for r in repos_js_entries) + "]"
    all_cmp_data = "[" + ",\n".join(r["cmp_data"] for r in repos_js_entries) + "]"
    all_diff_data = "[" + ",\n".join(r["diff_data"] for r in repos_js_entries) + "]"

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
  <meta http-equiv="Pragma" content="no-cache">
  <meta http-equiv="Expires" content="0">
  <title>BlankOn Linux Package Report</title>
  <style>
    /* Colours mirror the fumadocs tokens blankonlinux.id renders with
       (fumadocs-ui/css/colors/index.css): the bar is fd-background at 80%,
       a very light grey, over the white page. */
    :root {{
      --fg: hsl(0, 0%, 3.9%);
      --muted: hsl(0, 0%, 45.1%);
      --border: hsla(0, 0%, 80%, 0.5);
      --bg: #ffffff;
      --bar-bg: hsl(0, 0%, 96%);
      --accent-bg: hsla(0, 0%, 82%, 0.5);
      --accent-fg: hsl(0, 0%, 9%);
      --brand: #ff0000;
      --link: #1a73e8;
    }}
    * {{ box-sizing: border-box; }}
    html {{ -webkit-text-size-adjust: 100%; }}
    body {{
      margin: 0; color: var(--fg); background: var(--bg);
      -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen,
        Ubuntu, Cantarell, 'Fira Sans', 'Droid Sans', 'Helvetica Neue', sans-serif;
    }}

    /* ── top bar (mirrors blankonlinux.id) ── */
    .nav {{
      position: sticky; top: 0; z-index: 50;
      background: hsla(0, 0%, 96%, 0.8);
      backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
      border-bottom: 1px solid var(--border);
    }}
    .nav::after {{
      content: ''; position: absolute; left: 0; right: 0; bottom: -2px;
      height: 2px; pointer-events: none;
      background: linear-gradient(to right, transparent 0%, transparent 30%,
        var(--brand) 50%, transparent 70%, transparent 100%);
    }}
    .nav-inner {{
      max-width: 1400px; margin: 0 auto; min-height: 56px; padding: 0 1rem;
      display: flex; align-items: center; gap: 1rem;
    }}
    .nav-logo {{ height: 24px; width: auto; max-width: 100%; display: block; object-fit: contain; }}
    .nav-links {{ display: flex; align-items: center; gap: 0.5rem; padding: 0 1.5rem; }}
    .nav-links a, .nav-dd-btn {{
      display: inline-flex; align-items: center; gap: 0.35rem; padding: 0.5rem;
      font-family: inherit; font-size: 0.875rem; line-height: 1.2;
      color: var(--muted); text-decoration: none;
      background: none; border: 0; cursor: pointer; transition: color 0.15s;
    }}
    .nav-links a:hover, .nav-dd-btn:hover {{ color: var(--accent-fg); }}
    .nav-ext {{ width: 14px; height: 14px; flex-shrink: 0; opacity: 0.7; }}
    .nav-chevron {{ width: 12px; height: 12px; transition: transform 0.15s; }}
    .nav-dropdown.open .nav-chevron {{ transform: rotate(180deg); }}
    .nav-dropdown {{ position: relative; }}
    .nav-dd-menu {{ list-style: none; margin: 0; padding: 0.25rem 0; min-width: 170px; }}
    /* Override the inline-flex from .nav-links a, so the hover highlight
       spans the whole row instead of hugging the label. */
    .nav-dd-menu a {{ display: flex; width: 100%; }}
    .nav-dropdown:not(.open) .nav-dd-menu {{ display: none; }}
    .nav-burger {{
      display: none; margin-left: auto; padding: 0.5rem; color: var(--fg);
      background: none; border: 0; cursor: pointer;
    }}

    @media (min-width: 861px) {{
      .nav-dd-menu {{
        position: absolute; right: 0; top: 100%; margin-top: 0.5rem; z-index: 60;
        background: var(--bar-bg); border: 1px solid var(--border); border-radius: 6px;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
      }}
      /* Invisible bridge over the gap: without it the pointer leaves the
         dropdown on its way from the button to the panel and it snaps shut. */
      .nav-dd-menu::before {{
        content: ''; position: absolute; left: 0; right: 0;
        top: -0.5rem; height: 0.5rem;
      }}
      .nav-dd-menu a {{ padding: 0.5rem 1rem; }}
      .nav-dd-menu a:hover {{ background: var(--accent-bg); }}
    }}
    @media (max-width: 860px) {{
      .nav-burger {{ display: inline-flex; }}
      .nav-links {{
        display: none; position: absolute; left: 0; right: 0; top: 100%;
        flex-direction: column; align-items: stretch; gap: 0;
        padding: 0.5rem 1rem 1rem; background: var(--bar-bg);
        border-bottom: 1px solid var(--border);
        max-height: calc(100vh - 56px); overflow-y: auto;
      }}
      .nav.open .nav-links {{ display: flex; }}
      .nav-links a, .nav-dd-btn {{ padding: 0.65rem 0.25rem; font-size: 0.95rem; }}
      .nav-dd-btn {{ width: 100%; justify-content: space-between; }}
      .nav-dd-menu a {{ padding: 0.6rem 0.25rem 0.6rem 1rem; }}
    }}

    /* ── page ── */
    .container {{ max-width: 1400px; margin: 0 auto; padding: 1.5rem 1rem 2rem; }}
    h1 {{ font-size: 1.4rem; margin: 0 0 0.25rem; }}
    .meta {{ color: #666; font-size: 0.9rem; margin-bottom: 1rem; line-height: 1.7; overflow-wrap: anywhere; }}
    .tabs {{
      display: flex; gap: 0; border-bottom: 2px solid #ddd;
      overflow-x: auto; -webkit-overflow-scrolling: touch; scrollbar-width: none;
    }}
    .tabs::-webkit-scrollbar {{ display: none; }}
    .tab-btn {{
      padding: 0.5rem 1.2rem; cursor: pointer; border: 1px solid transparent;
      border-bottom: none; background: none; font-family: inherit; font-size: 0.95rem;
      color: #555; border-radius: 4px 4px 0 0; margin-bottom: -2px;
      white-space: nowrap; flex: 0 0 auto;
    }}
    .tab-btn:hover {{ background: #f4f4f4; }}
    .tab-btn.active {{
      border-color: #ddd; border-bottom-color: #fff; background: #fff;
      color: #222; font-weight: bold;
    }}
    .repo-panel {{ display: none; }}
    .repo-panel.active {{ display: block; }}
    .sub-panel {{ display: none; margin-top: 1rem; }}
    .sub-panel.active {{ display: block; }}
    .toolbar {{ display: flex; align-items: center; gap: 1rem; margin-bottom: 0.6rem; flex-wrap: wrap; }}
    .search-box {{
      padding: 0.4rem 0.7rem; font-family: inherit; font-size: 16px; border: 1px solid #ccc;
      border-radius: 4px; width: 280px; max-width: 100%; box-sizing: border-box;
    }}
    .row-count {{ color: #666; font-size: 0.85rem; }}
    .pagination {{ display: flex; align-items: center; gap: 0.3rem; flex-wrap: wrap; }}
    .pg-btn {{
      padding: 0.25rem 0.6rem; border: 1px solid #ccc; border-radius: 3px;
      background: #fff; cursor: pointer; font-family: inherit; font-size: 0.82rem; color: #333;
      min-width: 2rem;
    }}
    .pg-btn:hover {{ background: #f4f4f4; }}
    .pg-btn.active {{ background: var(--link); color: #fff; border-color: var(--link); font-weight: bold; }}
    .pg-btn:disabled {{ opacity: 0.4; cursor: default; }}
    .table-wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 0.88rem; }}
    th, td {{ border: 1px solid #ddd; padding: 0.45rem 0.65rem; vertical-align: top; }}
    th {{ background: #f4f4f4; text-align: left; white-space: nowrap; }}
    td {{ overflow-wrap: anywhere; }}
    tr:hover > td {{ background: #fafafa; }}
    .ver-above {{ color: #27ae60; font-weight: bold; }}
    .ver-below {{ color: #c0392b; font-weight: bold; }}
    .ver-missing {{ color: #e67e22; font-weight: bold; }}

    /* ── grouped diff ── */
    .group-by {{ display: inline-flex; align-items: center; gap: 0.4rem; color: #666; font-size: 0.85rem; }}
    .group-select {{
      padding: 0.35rem 0.5rem; font-family: inherit; font-size: 16px; color: #333;
      border: 1px solid #ccc; border-radius: 4px; background: #fff;
    }}
    .group-tools {{ display: inline-flex; gap: 0.3rem; }}
    .group-tools[hidden] {{ display: none; }}
    tr.grp {{ cursor: pointer; }}
    tr.grp > td:first-child {{ font-weight: 600; white-space: nowrap; }}
    tr.grp:hover > td {{ background: #f6f9ff; }}
    .chev {{ width: 12px; height: 12px; margin-right: 0.35rem; vertical-align: -1px;
      color: #777; transition: transform 0.12s; }}
    tr.grp.open .chev {{ transform: rotate(90deg); }}
    .chev-pad {{ display: inline-block; width: 12px; margin-right: 0.35rem; }}
    .grp-count {{
      display: inline-block; margin-left: 0.4rem; padding: 0 0.45rem; border-radius: 999px;
      background: #eef1f5; color: #555; font-size: 0.75rem; font-weight: 600; line-height: 1.5;
    }}
    tr.grp-child > td {{ background: #fcfcfc; }}
    tr.grp-child > td:first-child {{ padding-left: 2.1rem; }}
    .mixed {{ color: #888; font-style: italic; font-weight: normal; }}
    .grp-note {{ display: block; color: #888; font-weight: normal; font-size: 0.78rem; }}
    .grp-src {{ margin-left: 0.4rem; color: #999; font-weight: normal; font-size: 0.8rem; }}
    @media (prefers-reduced-motion: reduce) {{ .chev {{ transition: none; }} }}
    a {{ color: var(--link); }}
    footer {{ margin-top: 2rem; padding-top: 1rem; border-top: 1px solid #eee;
      color: #999; font-size: 0.82rem; overflow-wrap: anywhere; }}

    @media (max-width: 640px) {{
      .container {{ padding: 1rem 0.75rem 2rem; }}
      .tab-btn {{ padding: 0.5rem 0.85rem; font-size: 0.9rem; }}
      .search-box {{ flex: 1 1 100%; width: 100%; }}
      table {{ font-size: 0.82rem; }}
      th, td {{ padding: 0.4rem 0.5rem; }}
      /* Let wide tables scroll inside .table-wrap instead of squeezing columns. */
      .table-wrap table {{ min-width: 340px; }}
      .table-wrap table.cmp-table {{ min-width: 560px; }}
      .cmp-table td:last-child {{ white-space: nowrap; }}
    }}
  </style>
</head>
<body>
  <header class="nav" id="site-nav">
    <div class="nav-inner">
      <a href="https://blankonlinux.id/en" aria-label="BlankOn">
        <img class="nav-logo" src="https://blankonlinux.id/logo-black.png"
             alt="BlankOn" width="796" height="189">
      </a>
      <button class="nav-burger" type="button" aria-label="Menu"
              aria-expanded="false" onclick="toggleNav(this)">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="2" stroke-linecap="round">
          <path d="M4 7h16M4 12h16M4 17h16"/>
        </svg>
      </button>
      <nav class="nav-links">
        <a href="https://blankonlinux.id/en/download">Download</a>
        <a href="https://blankonlinux.id/en/wiki/">Wiki</a>
        <div class="nav-dropdown" id="dev-menu">
          <button class="nav-dd-btn" type="button" aria-haspopup="menu"
                  aria-expanded="false" onclick="toggleDevMenu(this)">
            Development
            <svg class="nav-chevron" viewBox="0 0 24 24" fill="none"
                 stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6"/></svg>
          </button>
          <ul class="nav-dd-menu">
            <li><a href="https://blankonlinux.id/en/team">Team</a></li>
            <li><a href="https://irgsh.blankonlinux.id/">IRGSH</a></li>
            <li><a href="https://packages.blankonlinux.id/">Packages</a></li>
            <li><a href="https://security.blankonlinux.id/">Security</a></li>
            <li><a href="https://jahitan.blankonlinux.id/" target="_blank" rel="noopener noreferrer">Jahitan{EXT_ICON}</a></li>
            <li><a href="https://arsip.blankonlinux.id/" target="_blank" rel="noopener noreferrer">Arsip{EXT_ICON}</a></li>
            <li><a href="https://arsip-dev.blankonlinux.id/" target="_blank" rel="noopener noreferrer">Arsip Dev{EXT_ICON}</a></li>
            <li><a href="https://github.com/blankon" target="_blank" rel="noopener noreferrer">Github{EXT_ICON}</a></li>
          </ul>
        </div>
        <a href="https://blankon.id/en/sponsorship" target="_blank" rel="noopener noreferrer">Sponsorship{EXT_ICON}</a>
        <a href="https://blankon.id/en/donate" target="_blank" rel="noopener noreferrer">Donate{EXT_ICON}</a>
      </nav>
    </div>
  </header>

  <div class="container">
  <h1>BlankOn Linux Package Report</h1>
  <div class="meta">
    Repositories: {repo_links}
    &nbsp;|&nbsp;
    Upstream: <a href="{e(upstream_url)}">{e(upstream_url)}</a> ({e(upstream_dist)})
    &nbsp;|&nbsp; Generated: {e(generated_at)}
  </div>

  <div class="tabs repo-tabs">
    {repo_tab_btns}
  </div>
  {repo_panels_html}

  <script>
    const ALL_PKG_DATA = {all_pkg_data};
    const ALL_CMP_DATA = {all_cmp_data};
    const ALL_DIFF_DATA = {all_diff_data};
    const UPSTREAM_PKG_DATA = {upstream_pkg_data};
    const PAGE_SIZE = 100;
    const TABLES = [];
    let UPSTREAM_TABLE;

    function escHtml(s) {{
      return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }}

    function rowMatches(r, lq) {{
      return Object.values(r).some(v => String(v).toLowerCase().includes(lq));
    }}

    // opts.filter(data, lq) and opts.count(filtered, allData) replace the row
    // search and the "N packages" counter, for tables whose items are groups.
    function makePaged(allData, tbodyId, topPagesId, botPagesId, countId, renderRow, opts = {{}}) {{
      const filterItems = opts.filter || ((data, lq) => data.filter(r => rowMatches(r, lq)));
      let query = '';
      let filtered = allData;
      let currentPage = 1;
      let searchTimer;

      function totalPages() {{ return Math.max(1, Math.ceil(filtered.length / PAGE_SIZE)); }}

      function render() {{
        const start = (currentPage - 1) * PAGE_SIZE;
        const slice = filtered.slice(start, start + PAGE_SIZE);
        document.getElementById(tbodyId).innerHTML = slice.map(renderRow).join('');
        const total = allData.length;
        const shown = filtered.length;
        document.getElementById(countId).textContent = (opts.count && opts.count(filtered, allData)) ||
          (shown === total ? total + ' packages' : shown + ' of ' + total + ' packages');
        renderPager(topPagesId);
        renderPager(botPagesId);
      }}

      function renderPager(id) {{
        const tp = totalPages();
        if (tp <= 1) {{ document.getElementById(id).innerHTML = ''; return; }}
        const MAX_BTNS = 9;
        let pages = [];
        if (tp <= MAX_BTNS) {{
          for (let i = 1; i <= tp; i++) pages.push(i);
        }} else {{
          pages = [1];
          let lo = Math.max(2, currentPage - 3);
          let hi = Math.min(tp - 1, currentPage + 3);
          if (lo > 2) pages.push('…');
          for (let i = lo; i <= hi; i++) pages.push(i);
          if (hi < tp - 1) pages.push('…');
          pages.push(tp);
        }}
        let html = '<button class="pg-btn" onclick="this._t.prev()" ' +
          (currentPage === 1 ? 'disabled' : '') + '>&#8249;</button>';
        pages.forEach(p => {{
          if (p === '…') {{
            html += '<span style="padding:0 0.2rem">…</span>';
          }} else {{
            html += '<button class="pg-btn' + (p === currentPage ? ' active' : '') +
              '" onclick="this._t.goto(' + p + ')">' + p + '</button>';
          }}
        }});
        html += '<button class="pg-btn" onclick="this._t.next()" ' +
          (currentPage === tp ? 'disabled' : '') + '>&#8250;</button>';
        const el = document.getElementById(id);
        el.innerHTML = html;
        el.querySelectorAll('button').forEach(b => b._t = obj);
      }}

      function applyFilter() {{
        const lq = query.toLowerCase();
        filtered = lq ? filterItems(allData, lq) : allData;
      }}

      // Wait for typing to pause: re-filtering ~78k rows on every key stutters.
      function search(q) {{
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {{
          query = q;
          applyFilter();
          currentPage = 1;
          render();
        }}, 150);
      }}

      const obj = {{
        search,
        setData(data) {{ allData = data; applyFilter(); currentPage = 1; render(); }},
        refresh: render,
        items() {{ return filtered; }},
        pageItems() {{
          const start = (currentPage - 1) * PAGE_SIZE;
          return filtered.slice(start, start + PAGE_SIZE);
        }},
        goto(p) {{ currentPage = Math.min(Math.max(1, p), totalPages()); render(); }},
        prev() {{ obj.goto(currentPage - 1); }},
        next() {{ obj.goto(currentPage + 1); }},
      }};

      render();
      return obj;
    }}

    const STATUS_CLASS = {{
      behind: 'ver-below', up_to_date: 'ver-above', not_in_repo: 'ver-missing',
      ahead: 'ver-above', not_in_upstream: ''
    }};
    const STATUS_LABEL = {{
      behind: 'Behind', up_to_date: 'Up to date', not_in_repo: 'Not in repo',
      ahead: 'Ahead', not_in_upstream: 'Not available in upstream'
    }};

    function cmpRow(r, trClass = '', before = '', after = '') {{
      const cls = STATUS_CLASS[r.s] || '';
      const lbl = STATUS_LABEL[r.s] || r.s;
      return '<tr' + (trClass ? ' class="' + trClass + '"' : '') + '>' +
        '<td>' + before + escHtml(r.n) + after + '</td>' +
        '<td class="' + cls + '">' + (escHtml(r.rv) || '—') + '</td>' +
        '<td>' + (escHtml(r.uv) || '—') + '</td>' +
        '<td class="' + cls + '">' + lbl + '</td>' +
        '</tr>';
    }}

    function renderCmpRow(r) {{ return cmpRow(r); }}

    // ── grouped Full Upstream Diff ──
    // Rows can be grouped by Debian source package or by the first one or two
    // dash-separated parts of the name; a group expands into its packages.
    const GROUP_KEY = {{
      source: r => r.src || r.n,
      prefix1: r => r.n.split('-')[0],
      prefix2: r => r.n.split('-').slice(0, 2).join('-'),
    }};
    const STATUS_RANK = {{ behind: 0, not_in_upstream: 1, ahead: 2 }};
    const AUTO_OPEN_MAX = 20;
    const CHEVRON = '<svg class="chev" viewBox="0 0 12 12" fill="none" stroke="currentColor" ' +
      'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M4.5 2.5 8 6l-3.5 3.5"/></svg>';

    function escAttr(s) {{ return escHtml(s).replace(/"/g, '&quot;'); }}

    function groupRows(rows, keyOf) {{
      const groups = new Map();
      for (const r of rows) {{
        const k = keyOf(r);
        if (!groups.has(k)) groups.set(k, []);
        groups.get(k).push(r);
      }}
      return [...groups]
        .sort((a, b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0)
        .map(([k, items]) => ({{ k, items, total: items.length }}));
    }}

    function sameOrMixed(values) {{
      const distinct = new Set(values).size;
      return distinct === 1
        ? (escHtml(values[0]) || '—')
        : '<span class="mixed">mixed (' + distinct + ')</span>';
    }}

    function makeDiffTable(i, rows) {{
      const tbodyId = 'r' + i + '-diff-tbody';
      const groupsByMode = {{}};
      const expanded = new Map();   // group key -> open?, set by the viewer
      let mode = 'none';

      // Small groups open by themselves while a search is active.
      function opensItself(g) {{ return g.searched && g.items.length <= AUTO_OPEN_MAX; }}
      function isOpen(g) {{ return expanded.has(g.k) ? expanded.get(g.k) : opensItself(g); }}

      function renderGroup(g) {{
        if (g.total === 1) {{
          // A group of one is just a package: nothing to expand.
          const r = g.items[0];
          return cmpRow(r, '', '<span class="chev-pad"></span>',
            r.n !== g.k ? '<span class="grp-src">' + escHtml(g.k) + '</span>' : '');
        }}
        const counts = {{}};
        g.items.forEach(r => counts[r.s] = (counts[r.s] || 0) + 1);
        const statuses = Object.keys(counts).sort((a, b) => STATUS_RANK[a] - STATUS_RANK[b]);
        const worst = statuses[0];
        const cls = STATUS_CLASS[worst] || '';
        const note = statuses.length > 1
          ? '<span class="grp-note">' +
            statuses.map(s => counts[s] + ' ' + STATUS_LABEL[s].toLowerCase()).join(' · ') + '</span>'
          : '';
        const open = isOpen(g);
        const count = g.items.length === g.total ? g.total : g.items.length + '/' + g.total;
        let html = '<tr class="grp' + (open ? ' open' : '') + '" tabindex="0" ' +
          'aria-expanded="' + open + '" data-k="' + escAttr(g.k) + '">' +
          '<td>' + CHEVRON + escHtml(g.k) + '<span class="grp-count">' + count + '</span></td>' +
          '<td class="' + cls + '">' + sameOrMixed(g.items.map(r => r.rv)) + '</td>' +
          '<td>' + sameOrMixed(g.items.map(r => r.uv)) + '</td>' +
          '<td class="' + cls + '">' + STATUS_LABEL[worst] + note + '</td></tr>';
        if (open) html += g.items.map(r => cmpRow(r, 'grp-child')).join('');
        return html;
      }}

      function filterGroups(groups, lq) {{
        const out = [];
        for (const g of groups) {{
          const items = g.items.filter(r => rowMatches(r, lq));
          if (items.length) out.push({{ k: g.k, items, total: g.total, searched: true }});
        }}
        return out;
      }}

      function countGroups(shown, all) {{
        const packages = shown.reduce((n, g) => n + g.items.length, 0);
        return shown === all
          ? all.length + ' groups · ' + rows.length + ' packages'
          : shown.length + ' of ' + all.length + ' groups · ' + packages + ' of ' + rows.length + ' packages';
      }}

      const table = makePaged(
        rows,
        tbodyId, 'r' + i + '-diff-pages', 'r' + i + '-diff-pages-bottom', 'r' + i + '-diff-count',
        x => mode === 'none' ? renderCmpRow(x) : renderGroup(x),
        {{
          filter: (data, lq) => mode === 'none' ? data.filter(r => rowMatches(r, lq)) : filterGroups(data, lq),
          count: (shown, all) => mode === 'none' ? null : countGroups(shown, all),
        }}
      );

      const tbody = document.getElementById(tbodyId);
      function toggle(tr, refocus) {{
        const k = tr.dataset.k;
        expanded.set(k, !tr.classList.contains('open'));
        table.refresh();
        if (refocus) {{
          const again = [...tbody.querySelectorAll('tr.grp')].find(el => el.dataset.k === k);
          if (again) again.focus();
        }}
      }}
      tbody.addEventListener('click', e => {{
        const tr = e.target.closest('tr.grp');
        if (tr) toggle(tr, false);
      }});
      tbody.addEventListener('keydown', e => {{
        const tr = e.target.closest('tr.grp');
        if (tr && (e.key === 'Enter' || e.key === ' ')) {{
          e.preventDefault();
          toggle(tr, true);
        }}
      }});

      return {{
        search: table.search,
        groupBy(m) {{
          mode = m;
          expanded.clear();
          document.getElementById('r' + i + '-diff-group-tools').hidden = m === 'none';
          if (m !== 'none' && !groupsByMode[m]) groupsByMode[m] = groupRows(rows, GROUP_KEY[m]);
          table.setData(m === 'none' ? rows : groupsByMode[m]);
        }},
        expandPage() {{
          table.pageItems().forEach(g => {{ if (g.total > 1) expanded.set(g.k, true); }});
          table.refresh();
        }},
        collapseAll() {{
          // Forget earlier choices and keep only the groups a search opened shut,
          // so a later search still opens its matches.
          expanded.clear();
          table.items().forEach(g => {{ if (opensItself(g)) expanded.set(g.k, false); }});
          table.refresh();
        }},
      }};
    }}

    ALL_PKG_DATA.forEach((pkgData, i) => {{
      const pkg = makePaged(
        pkgData,
        'r' + i + '-pkg-tbody', 'r' + i + '-pkg-pages', 'r' + i + '-pkg-pages-bottom', 'r' + i + '-pkg-count',
        r => '<tr><td>' + (r.u
          ? '<a href="' + escHtml(r.u.substring(0, r.u.lastIndexOf('/') + 1)) + '">' + escHtml(r.n) + '</a>'
          : escHtml(r.n)) + '</td><td>' + escHtml(r.v) + '</td></tr>'
      );
      const cmp = makePaged(
        ALL_CMP_DATA[i],
        'r' + i + '-cmp-tbody', 'r' + i + '-cmp-pages', 'r' + i + '-cmp-pages-bottom', 'r' + i + '-cmp-count',
        renderCmpRow
      );
      const diff = makeDiffTable(i, ALL_DIFF_DATA[i]);
      TABLES.push({{ pkg, cmp, diff }});
    }});

    UPSTREAM_TABLE = makePaged(
      UPSTREAM_PKG_DATA,
      'upstream-pkg-tbody', 'upstream-pkg-pages', 'upstream-pkg-pages-bottom', 'upstream-pkg-count',
      r => '<tr><td>' + (r.u
        ? '<a href="' + escHtml(r.u.substring(0, r.u.lastIndexOf('/') + 1)) + '">' + escHtml(r.n) + '</a>'
        : escHtml(r.n)) + '</td><td>' + escHtml(r.v) + '</td></tr>'
    );

    function switchRepoTab(idx, btn) {{
      document.querySelectorAll('.repo-panel').forEach(p => p.classList.remove('active'));
      document.querySelectorAll('.repo-tab-btn').forEach(b => b.classList.remove('active'));
      document.getElementById('repo-' + idx).classList.add('active');
      btn.classList.add('active');
    }}

    function switchSubTab(panelId, btn, repoIdx) {{
      const repoPanel = document.getElementById('repo-' + String(repoIdx));
      repoPanel.querySelectorAll('.sub-panel').forEach(p => p.classList.remove('active'));
      repoPanel.querySelectorAll('.sub-tab-btn').forEach(b => b.classList.remove('active'));
      document.getElementById(panelId).classList.add('active');
      btn.classList.add('active');
    }}

    // ── top bar ──
    const NAV = document.getElementById('site-nav');
    const DEV_MENU = document.getElementById('dev-menu');
    const canHover = () => window.matchMedia('(hover: hover)').matches;

    function setDevMenu(open) {{
      DEV_MENU.classList.toggle('open', open);
      DEV_MENU.querySelector('.nav-dd-btn').setAttribute('aria-expanded', String(open));
    }}

    function toggleDevMenu(btn) {{
      clearTimeout(devCloseTimer);
      setDevMenu(canHover() ? true : !DEV_MENU.classList.contains('open'));
    }}

    function toggleNav(btn) {{
      const open = !NAV.classList.contains('open');
      NAV.classList.toggle('open', open);
      btn.setAttribute('aria-expanded', String(open));
      if (!open) setDevMenu(false);
    }}

    // Pointer devices open the Development menu on hover; touch devices tap it.
    // Closing is delayed so a slow diagonal move onto the panel does not lose it.
    let devCloseTimer;
    DEV_MENU.addEventListener('mouseenter', () => {{
      clearTimeout(devCloseTimer);
      if (canHover()) setDevMenu(true);
    }});
    DEV_MENU.addEventListener('mouseleave', () => {{
      if (!canHover()) return;
      clearTimeout(devCloseTimer);
      devCloseTimer = setTimeout(() => setDevMenu(false), 100);
    }});

    document.addEventListener('pointerdown', (event) => {{
      if (!DEV_MENU.contains(event.target)) setDevMenu(false);
      if (!NAV.contains(event.target)) {{
        NAV.classList.remove('open');
        NAV.querySelector('.nav-burger').setAttribute('aria-expanded', 'false');
      }}
    }});
    document.addEventListener('keydown', (event) => {{
      if (event.key !== 'Escape') return;
      setDevMenu(false);
      NAV.classList.remove('open');
      NAV.querySelector('.nav-burger').setAttribute('aria-expanded', 'false');
    }});
  </script>
  <footer>
    Source code: <a href="https://github.com/blankon/untung">https://github.com/blankon/untung</a>
  </footer>
  </div>
</body>
</html>
"""

    os.makedirs(html_dir, exist_ok=True)
    out_path = os.path.join(html_dir, "index.html")
    with open(out_path, "w") as f:
        f.write(page)
    print(f"HTML report written to {out_path}", file=sys.stderr)


# ── entry point ───────────────────────────────────────────────────────────────

USAGE = """Usage:
  untung.py --repo=<url>[@<dist>] [--repo=...] --upstream-repo=<url>[@<dist>] [--html=<dir>]

A repo may name its suite after an '@', e.g.
  --repo=http://arsip.blankonlinux.id/@verbeek
Without one, every dist under {repo}/dists/ is indexed and merged.
The upstream dist defaults to '%s'.""" % UPSTREAM_DEFAULT_DIST


def parse_repo_arg(value):
    """Split "<url>[@<dist>]" into (url, dist or None).

    Only a trailing '@dist' counts: the separator must come after the last '/'
    so that userinfo in a URL (http://user@host/) is left alone.
    """
    at = value.rfind("@")
    if at == -1 or "/" in value[at + 1:] or not value[at + 1:]:
        return value, None
    return value[:at], value[at + 1:]


def main():
    repos = []            # list of (url, dist or None)
    upstream_repo = None
    upstream_dist = UPSTREAM_DEFAULT_DIST
    html_dir = None

    for arg in sys.argv[1:]:
        if arg.startswith("--repo=") or arg.startswith("--repository="):
            repos.append(parse_repo_arg(arg.split("=", 1)[1]))
        elif arg.startswith("--upstream-repo="):
            upstream_repo, dist = parse_repo_arg(arg.split("=", 1)[1])
            if dist:
                upstream_dist = dist
        elif arg.startswith("--upstream-dist="):
            upstream_dist = arg.split("=", 1)[1]
        elif arg.startswith("--html="):
            html_dir = arg.split("=", 1)[1]
        elif arg in ("-h", "--help"):
            print(USAGE)
            sys.exit(0)

    if not repos:
        print("Error: at least one --repo=<url> is required\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        sys.exit(1)
    if not upstream_repo:
        print("Error: --upstream-repo=<url> is required\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        sys.exit(1)

    packages = fetch_package_list()
    upstream_index = build_upstream_index(upstream_repo, dist=upstream_dist)

    repo_data_list = []
    for repo_url, repo_dist in repos:
        repo_index = build_package_index(repo_url, dist=repo_dist)

        print(f"Comparing versions for {repo_url} ...", file=sys.stderr)
        results = compare_versions(packages, repo_index, upstream_index)

        behind = [r for r in results if r["status"] == "behind"]
        not_in_repo = [r for r in results if r["status"] == "not_in_repo"]

        if not behind and not not_in_repo:
            print(f"  All packages are up to date with upstream.", file=sys.stderr)
        else:
            print(f"  {len(behind)} package(s) behind upstream:", file=sys.stderr)
            for r in behind:
                print(f"    {r['package']}: {r['repo_version']} < {r['upstream_version']}", file=sys.stderr)
            if not_in_repo:
                print(f"  {len(not_in_repo)} package(s) not found in repo.", file=sys.stderr)

        diff = compare_repo_diff(repo_index, upstream_index)
        print(f"  {len(diff)} of {len(repo_index)} repo package(s) differ from upstream.", file=sys.stderr)

        repo_data_list.append({
            "url": repo_url,
            "dist": repo_dist,
            "index": repo_index,
            "results": results,
            "diff": diff,
        })

    if html_dir:
        write_html_report(repo_data_list, html_dir, upstream_repo, upstream_index,
                          upstream_dist)


if __name__ == "__main__":
    main()
