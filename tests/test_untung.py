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


class HtmlDiffDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def diff_rows(self, diff):
        with contextlib.redirect_stderr(io.StringIO()):
            untung.write_html_report(
                [{"url": "http://repo.example/", "index": {}, "results": [], "diff": diff}],
                self.tmp, "http://upstream.example/debian/",
            )
        page = Path(self.tmp, "index.html").read_text()
        data = re.search(r"const ALL_DIFF_DATA = (.*?);\n", page, re.S).group(1)
        return {r["n"]: r for r in json.loads(data)[0]}

    def test_diff_rows_name_source_only_when_it_differs(self):
        rows = self.diff_rows([
            {"package": "dnsmasq", "repo_version": "2.93-1", "repo_url": "u",
             "upstream_version": "2.93-3", "status": "behind", "source": "dnsmasq"},
            {"package": "dnsmasq-base", "repo_version": "2.93-1", "repo_url": "u",
             "upstream_version": "2.93-3", "status": "behind", "source": "dnsmasq"},
        ])
        self.assertNotIn("src", rows["dnsmasq"])
        self.assertEqual(rows["dnsmasq-base"]["src"], "dnsmasq")


if __name__ == "__main__":
    unittest.main()
