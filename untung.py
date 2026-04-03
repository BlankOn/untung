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
    """Fetch and parse Packages.gz; return dict of binary package -> {version, url}."""
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

    for line in text.splitlines():
        if line.startswith("Package:"):
            current_pkg = line.split(":", 1)[1].strip()
            current_ver = None
            current_filename = None
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
                    }
    return packages


def build_package_index(repo_url, arch="amd64"):
    """
    Walk all dists and components in the repo and return a unified
    binary package -> {version, url} map.
    """
    print(f"Discovering dists at {repo_url} ...", file=sys.stderr)
    dists = discover_dists(repo_url)
    if not dists:
        print("Error: no dists found.", file=sys.stderr)
        sys.exit(1)
    print(f"  Found dists: {', '.join(dists)}", file=sys.stderr)

    index = {}
    for dist in dists:
        _, components = fetch_release(repo_url, dist)
        for component in components:
            print(f"  Fetching {dist}/{component}/binary-{arch}/Packages.gz ...", file=sys.stderr)
            pkgs = fetch_packages(repo_url, dist, component, arch)
            for pkg, info in pkgs.items():
                existing = index.get(pkg)
                if existing is None or version_lt(existing["version"], info["version"]):
                    index[pkg] = info

    print(f"  Indexed {len(index)} binary packages.", file=sys.stderr)
    return index


def build_upstream_index(repo_url, arch="amd64"):
    """Fetch binary package versions from the 'sid' dist of an upstream repo."""
    print(f"Fetching sid index from upstream {repo_url} ...", file=sys.stderr)
    _, components = fetch_release(repo_url, "sid")
    if not components:
        print("  Warning: sid release not found or has no components.", file=sys.stderr)
        return {}

    index = {}
    for component in components:
        print(f"  Fetching sid/{component}/binary-{arch}/Packages.gz ...", file=sys.stderr)
        pkgs = fetch_packages(repo_url, "sid", component, arch)
        for pkg, info in pkgs.items():
            existing = index.get(pkg)
            if existing is None or version_lt(existing["version"], info["version"]):
                index[pkg] = info

    print(f"  Indexed {len(index)} binary packages from sid.", file=sys.stderr)
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


# ── html report ───────────────────────────────────────────────────────────────

def write_html_report(results, html_dir, repo_url, upstream_url, repo_index):
    import html as _html
    import json as _json

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def e(s):
        return _html.escape(str(s)) if s is not None else ""

    count_behind = sum(1 for r in results if r["status"] == "behind")
    summary = (
        f"{count_behind} package(s) behind upstream."
        if count_behind
        else "All packages are up to date with upstream."
    )
    summary_color = "#c0392b" if count_behind else "#27ae60"

    # Embed data as JSON; JS handles rendering and pagination
    pkg_data = _json.dumps(
        [{"n": k, "v": info["version"], "u": info["url"]} for k, info in sorted(repo_index.items())]
    )
    cmp_data = _json.dumps([
        {
            "n": r["package"],
            "rv": r["repo_version"] or "",
            "uv": r["upstream_version"] or "",
            "s": r["status"],
        }
        for r in (
            [r for r in results if r["status"] == "behind"] +
            [r for r in results if r["status"] == "not_in_repo"] +
            [r for r in results if r["status"] == "not_in_upstream"] +
            [r for r in results if r["status"] == "up_to_date"]
        )
    ])

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
    body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #222; }}
    h1 {{ font-size: 1.4rem; margin-bottom: 0.25rem; }}
    .meta {{ color: #666; font-size: 0.9rem; margin-bottom: 1rem; }}
    .summary {{ font-weight: bold; margin-bottom: 1rem; color: {summary_color}; }}
    .tabs {{ display: flex; gap: 0; margin-bottom: 1.5rem; border-bottom: 2px solid #ddd; }}
    .tab-btn {{
      padding: 0.5rem 1.2rem; cursor: pointer; border: 1px solid transparent;
      border-bottom: none; background: none; font-size: 0.95rem; color: #555;
      border-radius: 4px 4px 0 0; margin-bottom: -2px;
    }}
    .tab-btn:hover {{ background: #f4f4f4; }}
    .tab-btn.active {{
      border-color: #ddd; border-bottom-color: #fff; background: #fff;
      color: #222; font-weight: bold;
    }}
    .tab-panel {{ display: none; }}
    .tab-panel.active {{ display: block; }}
    .toolbar {{ display: flex; align-items: center; gap: 1rem; margin-bottom: 0.6rem; flex-wrap: wrap; }}
    .search-box {{
      padding: 0.4rem 0.7rem; font-size: 0.9rem; border: 1px solid #ccc;
      border-radius: 4px; width: 280px; box-sizing: border-box;
    }}
    .row-count {{ color: #666; font-size: 0.85rem; }}
    .pagination {{ display: flex; align-items: center; gap: 0.3rem; flex-wrap: wrap; }}
    .pg-btn {{
      padding: 0.25rem 0.6rem; border: 1px solid #ccc; border-radius: 3px;
      background: #fff; cursor: pointer; font-size: 0.82rem; color: #333;
    }}
    .pg-btn:hover {{ background: #f4f4f4; }}
    .pg-btn.active {{ background: #1a73e8; color: #fff; border-color: #1a73e8; font-weight: bold; }}
    .pg-btn:disabled {{ opacity: 0.4; cursor: default; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 0.88rem; }}
    th, td {{ border: 1px solid #ddd; padding: 0.45rem 0.65rem; vertical-align: top; }}
    th {{ background: #f4f4f4; text-align: left; white-space: nowrap; }}
    tr:hover > td {{ background: #fafafa; }}
    .ver-above {{ color: #27ae60; font-weight: bold; }}
    .ver-below {{ color: #c0392b; font-weight: bold; }}
    .ver-missing {{ color: #e67e22; font-weight: bold; }}
    a {{ color: #1a73e8; }}
  </style>
</head>
<body>
  <h1>BlankOn Linux Package Report</h1>
  <div class="meta">
    Repository: <a href="{e(repo_url)}" target="_blank">{e(repo_url)}</a>
    &nbsp;|&nbsp;
    Upstream: <a href="{e(upstream_url)}" target="_blank">{e(upstream_url)}</a>
    &nbsp;|&nbsp; Generated: {e(generated_at)}
  </div>

  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('pkg-list', this)">Package List</button>
    <button class="tab-btn" onclick="switchTab('upstream-cmp', this)">Upstream Comparison</button>
  </div>

  <!-- Tab 1: Package List -->
  <div id="pkg-list" class="tab-panel active">
    <div class="toolbar">
      <input class="search-box" type="search" id="pkg-search"
             placeholder="Search packages..." oninput="pkgTable.search(this.value)">
      <span class="row-count" id="pkg-count"></span>
    </div>
    <div class="pagination" id="pkg-pages" style="margin-bottom:0.6rem"></div>
    <table>
      <thead><tr><th>Package</th><th>Version</th></tr></thead>
      <tbody id="pkg-tbody"><tr><td colspan="2" style="color:#999;font-style:italic">Loading...</td></tr></tbody>
    </table>
    <div class="pagination" id="pkg-pages-bottom" style="margin-top:0.6rem"></div>
  </div>

  <!-- Tab 2: Upstream Comparison -->
  <div id="upstream-cmp" class="tab-panel">
    <div class="summary" id="cmp-summary">{e(summary)}</div>
    <div class="toolbar">
      <input class="search-box" type="search" id="cmp-search"
             placeholder="Search packages..." oninput="cmpTable.search(this.value)">
      <span class="row-count" id="cmp-count"></span>
      <span class="row-count">from <a href="https://github.com/BlankOn/blankon-live-build/tree/main/config/package-lists" target="_blank">https://github.com/BlankOn/blankon-live-build/tree/main/config/package-lists</a></span>
    </div>
    <div class="pagination" id="cmp-pages" style="margin-bottom:0.6rem"></div>
    <table>
      <thead>
        <tr>
          <th>Package</th><th>Our version</th><th>Upstream version (Sid)</th><th>Status</th>
        </tr>
      </thead>
      <tbody id="cmp-tbody"><tr><td colspan="4" style="color:#999;font-style:italic">Loading...</td></tr></tbody>
    </table>
    <div class="pagination" id="cmp-pages-bottom" style="margin-top:0.6rem"></div>
  </div>

  <script>
    const PKG_DATA = {pkg_data};
    const CMP_DATA = {cmp_data};
    const PAGE_SIZE = 100;

    function escHtml(s) {{
      return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }}

    function makePaged(allData, tbodyId, topPagesId, botPagesId, countId, renderRow) {{
      let filtered = allData;
      let currentPage = 1;

      function totalPages() {{ return Math.max(1, Math.ceil(filtered.length / PAGE_SIZE)); }}

      function render() {{
        const start = (currentPage - 1) * PAGE_SIZE;
        const slice = filtered.slice(start, start + PAGE_SIZE);
        document.getElementById(tbodyId).innerHTML = slice.map(renderRow).join('');
        const total = allData.length;
        const shown = filtered.length;
        document.getElementById(countId).textContent =
          shown === total ? total + ' packages' : shown + ' of ' + total + ' packages';
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

      function search(q) {{
        const lq = q.toLowerCase();
        filtered = lq ? allData.filter(r =>
          Object.values(r).some(v => String(v).toLowerCase().includes(lq))
        ) : allData;
        currentPage = 1;
        render();
      }}

      const obj = {{
        search,
        goto(p) {{ currentPage = Math.min(Math.max(1, p), totalPages()); render(); }},
        prev() {{ obj.goto(currentPage - 1); }},
        next() {{ obj.goto(currentPage + 1); }},
      }};

      render();
      return obj;
    }}

    const pkgTable = makePaged(
      PKG_DATA,
      'pkg-tbody', 'pkg-pages', 'pkg-pages-bottom', 'pkg-count',
      r => '<tr><td>' + (r.u ? '<a href="' + escHtml(r.u.substring(0, r.u.lastIndexOf('/') + 1)) + '" target="_blank">' + escHtml(r.n) + '</a>' : escHtml(r.n)) + '</td><td>' + escHtml(r.v) + '</td></tr>'
    );

    const STATUS_CLASS = {{
      behind: 'ver-below', up_to_date: 'ver-above', not_in_repo: 'ver-missing', not_in_upstream: ''
    }};
    const STATUS_LABEL = {{
      behind: 'Behind', up_to_date: 'Up to date', not_in_repo: 'Not in repo', not_in_upstream: 'Not available in upstream'
    }};

    const cmpTable = makePaged(
      CMP_DATA,
      'cmp-tbody', 'cmp-pages', 'cmp-pages-bottom', 'cmp-count',
      r => {{
        const cls = STATUS_CLASS[r.s] || '';
        const lbl = STATUS_LABEL[r.s] || r.s;
        return '<tr>' +
          '<td>' + escHtml(r.n) + '</td>' +
          '<td class="' + cls + '">' + (escHtml(r.rv) || '—') + '</td>' +
          '<td>' + (escHtml(r.uv) || '—') + '</td>' +
          '<td class="' + cls + '">' + lbl + '</td>' +
          '</tr>';
      }}
    );

    function switchTab(id, btn) {{
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.getElementById(id).classList.add('active');
      btn.classList.add('active');
    }}
  </script>
  <footer style="margin-top:2rem; padding-top:1rem; border-top:1px solid #eee; color:#999; font-size:0.82rem;">
    Source code: <a href="https://github.com/blankon/untung" target="_blank">https://github.com/blankon/untung</a>
  </footer>
</body>
</html>
"""

    os.makedirs(html_dir, exist_ok=True)
    out_path = os.path.join(html_dir, "index.html")
    with open(out_path, "w") as f:
        f.write(page)
    print(f"HTML report written to {out_path}", file=sys.stderr)


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    repo_url = None
    upstream_repo = None
    html_dir = None

    for arg in sys.argv[1:]:
        if arg.startswith("--repo=") or arg.startswith("--repository="):
            repo_url = arg.split("=", 1)[1]
        elif arg.startswith("--upstream-repo="):
            upstream_repo = arg.split("=", 1)[1]
        elif arg.startswith("--html="):
            html_dir = arg.split("=", 1)[1]

    if not repo_url:
        print("Error: --repo=<url> is required", file=sys.stderr)
        sys.exit(1)
    if not upstream_repo:
        print("Error: --upstream-repo=<url> is required", file=sys.stderr)
        sys.exit(1)

    packages = fetch_package_list()

    repo_index = build_package_index(repo_url)
    upstream_index = build_upstream_index(upstream_repo)

    print("Comparing versions ...", file=sys.stderr)
    results = compare_versions(packages, repo_index, upstream_index)

    behind = [r for r in results if r["status"] == "behind"]
    not_in_repo = [r for r in results if r["status"] == "not_in_repo"]

    if not behind and not not_in_repo:
        print("All packages are up to date with upstream.", file=sys.stderr)
    else:
        print(f"\n{len(behind)} package(s) behind upstream:\n", file=sys.stderr)
        for r in behind:
            print(f"  {r['package']}")
            print(f"    Our version      : {r['repo_version']}")
            print(f"    Upstream version : {r['upstream_version']}")
        if not_in_repo:
            print(f"\n{len(not_in_repo)} package(s) not found in repo:", file=sys.stderr)
            for r in not_in_repo:
                print(f"  {r['package']} (upstream: {r['upstream_version']})", file=sys.stderr)

    if html_dir:
        write_html_report(results, html_dir, repo_url, upstream_repo, repo_index)


if __name__ == "__main__":
    main()
