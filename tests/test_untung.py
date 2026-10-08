import contextlib
import gzip
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import untung  # noqa: E402


PACKAGES = """\
Package: dnsmasq
Version: 2.93-1
Architecture: all
Filename: pool/main/d/dnsmasq/dnsmasq_2.93-1_all.deb

Package: dnsmasq-base
Source: dnsmasq
Version: 2.93-1
Architecture: amd64
Filename: pool/main/d/dnsmasq/dnsmasq-base_2.93-1_amd64.deb

Package: libfoo1
Source: foo (1.2-3)
Version: 1.2-3+b1
Architecture: amd64
Filename: pool/main/f/foo/libfoo1_1.2-3+b1_amd64.deb
"""


def make_repo(root, dist, component, text):
    """Lay out a minimal apt repo with one Packages.gz and return its file:// URL."""
    pkg_dir = Path(root, "dists", dist, component, "binary-amd64")
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "Packages.gz").write_bytes(gzip.compress(text.encode()))
    return Path(root).as_uri() + "/"


class FetchPackagesSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        url = make_repo(self.tmp, "sid", "main", PACKAGES)
        self.packages = untung.fetch_packages(url, "sid", "main")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_binary_takes_source_from_source_field(self):
        self.assertEqual(self.packages["dnsmasq-base"]["source"], "dnsmasq")

    def test_source_version_suffix_is_dropped(self):
        self.assertEqual(self.packages["libfoo1"]["source"], "foo")

    def test_missing_source_field_means_same_name(self):
        self.assertEqual(self.packages["dnsmasq"]["source"], "dnsmasq")


class CompareRepoDiffSourceTest(unittest.TestCase):
    def test_not_in_upstream_row_carries_source(self):
        repo_index = {"libfoo1": {"version": "1.2-3+b1", "url": "u", "source": "foo"}}
        [row] = untung.compare_repo_diff(repo_index, {})
        self.assertEqual(row["status"], "not_in_upstream")
        self.assertEqual(row["source"], "foo")

    @unittest.skipUnless(shutil.which("dpkg"), "needs dpkg --compare-versions")
    def test_behind_row_carries_source(self):
        repo_index = {"dnsmasq-base": {"version": "2.93-1", "url": "u", "source": "dnsmasq"}}
        upstream_index = {"dnsmasq-base": {"version": "2.93-3", "url": "u", "source": "dnsmasq"}}
        [row] = untung.compare_repo_diff(repo_index, upstream_index)
        self.assertEqual(row["status"], "behind")
        self.assertEqual(row["source"], "dnsmasq")


def render_report(tmp, repos, upstream_url="http://upstream.example/debian/", upstream_index=None):
    """Write the report into tmp and return the page text."""
    with contextlib.redirect_stderr(io.StringIO()):
        untung.write_html_report(repos, tmp, upstream_url, upstream_index)
    return Path(tmp, "index.html").read_text(encoding="utf-8")


def page_const(page, name):
    """Return the JSON value the page assigns to `const <name>`."""
    return json.loads(re.search(r"const %s = (.*?);\n" % name, page, re.S).group(1))


def repo(url="http://repo.example/blankon/", index=None, results=None, diff=None):
    return {"url": url, "index": index or {}, "results": results or [], "diff": diff or []}


class HtmlReportDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_package_rows_store_folder_relative_to_repo(self):
        page = render_report(self.tmp, [repo(index={"dnsmasq": {
            "version": "2.93-1",
            "url": "http://repo.example/blankon/pool/main/d/dnsmasq/dnsmasq_2.93-1_all.deb",
        }})])
        self.assertEqual(page_const(page, "REPO_BASES"), ["http://repo.example/blankon/"])
        self.assertEqual(page_const(page, "ALL_PKG_DATA"), [[["dnsmasq", "2.93-1", "pool/main/d/dnsmasq/"]]])

    def test_package_outside_repo_keeps_absolute_folder(self):
        page = render_report(self.tmp, [repo(index={"odd": {
            "version": "1", "url": "http://elsewhere.example/pool/o/odd/odd_1_all.deb",
        }})])
        self.assertEqual(page_const(page, "ALL_PKG_DATA"), [[["odd", "1", "http://elsewhere.example/pool/o/odd/"]]])

    def test_upstream_rows_store_folder_relative_to_upstream(self):
        page = render_report(self.tmp, [repo()], "https://mirror.example/debian/", {"dnsmasq": {
            "version": "2.93-3",
            "url": "https://mirror.example/debian/pool/main/d/dnsmasq/dnsmasq_2.93-3_all.deb",
        }})
        self.assertEqual(page_const(page, "UPSTREAM_BASE"), "https://mirror.example/debian/")
        self.assertEqual(page_const(page, "UPSTREAM_PKG_DATA"), [["dnsmasq", "2.93-3", "pool/main/d/dnsmasq/"]])

    def test_comparison_rows_use_status_codes(self):
        page = render_report(self.tmp, [repo(results=[
            {"package": "dpkg", "repo_version": "1.23.7", "repo_url": "u",
             "upstream_version": "1.23.11", "status": "behind"},
            {"package": "bash", "repo_version": "5.3-1", "repo_url": "u",
             "upstream_version": "5.3-1", "status": "up_to_date"},
            {"package": "gone", "repo_version": None, "repo_url": None,
             "upstream_version": "1.0-1", "status": "not_in_repo"},
            {"package": "blankon-keyring", "repo_version": "2026.1", "repo_url": "u",
             "upstream_version": None, "status": "not_in_upstream"},
        ])])
        self.assertEqual(page_const(page, "ALL_CMP_DATA"), [[
            ["dpkg", "1.23.7", "1.23.11", "b"],
            ["gone", "", "1.0-1", "r"],
            ["blankon-keyring", "2026.1", "", "n"],
            ["bash", "5.3-1", "5.3-1", "u"],
        ]])

    def test_diff_rows_name_source_only_when_it_differs(self):
        page = render_report(self.tmp, [repo(diff=[
            {"package": "dnsmasq", "repo_version": "2.93-1", "repo_url": "u",
             "upstream_version": "2.93-3", "status": "behind", "source": "dnsmasq"},
            {"package": "dnsmasq-base", "repo_version": "2.93-1", "repo_url": "u",
             "upstream_version": "2.93-3", "status": "behind", "source": "dnsmasq"},
            {"package": "mutter", "repo_version": "49.1-2", "repo_url": "u",
             "upstream_version": "49.0-1", "status": "ahead", "source": "mutter"},
        ])])
        self.assertEqual(page_const(page, "ALL_DIFF_DATA"), [[
            ["dnsmasq", "2.93-1", "2.93-3", "b"],
            ["dnsmasq-base", "2.93-1", "2.93-3", "b", "dnsmasq"],
            ["mutter", "49.1-2", "49.0-1", "a"],
        ]])

    def test_report_writes_matching_gzip_copy(self):
        render_report(self.tmp, [repo()])
        page = Path(self.tmp, "index.html").read_bytes()
        packed = Path(self.tmp, "index.html.gz").read_bytes()
        self.assertEqual(gzip.decompress(packed), page)


if __name__ == "__main__":
    unittest.main()
