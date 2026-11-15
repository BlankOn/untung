import contextlib
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
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


class DataFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_file_holds_gzipped_compact_json(self):
        rel = untung.write_data_file(self.tmp, "r0-arsip-pkg", [["dnsmasq", "2.93-1", "pool/main/d/dnsmasq/"]])
        self.assertTrue(rel.startswith("data/r0-arsip-pkg.") and rel.endswith(".json.gz"))
        raw = gzip.decompress(Path(self.tmp, rel).read_bytes())
        self.assertEqual(raw, b'[["dnsmasq","2.93-1","pool/main/d/dnsmasq/"]]')

    def test_name_carries_12_char_content_hash(self):
        json_bytes, _ = untung.encode_rows([["a", "1", ""]])
        name = untung.data_file_name("x", json_bytes)
        self.assertRegex(name, r"^x\.[0-9a-f]{12}\.json\.gz$")

    def test_same_rows_same_bytes(self):
        self.assertEqual(untung.encode_rows([["a", "1", ""]]), untung.encode_rows([["a", "1", ""]]))

    def test_different_rows_different_name(self):
        a = untung.write_data_file(self.tmp, "x", [["a", "1", ""]])
        b = untung.write_data_file(self.tmp, "x", [["a", "2", ""]])
        self.assertNotEqual(a, b)

    def test_existing_file_is_not_rewritten(self):
        rel = untung.write_data_file(self.tmp, "x", [["a", "1", ""]])
        path = Path(self.tmp, rel)
        os.utime(path, (1_000_000, 1_000_000))
        untung.write_data_file(self.tmp, "x", [["a", "1", ""]])
        self.assertEqual(path.stat().st_mtime, 1_000_000)

    def test_files_are_world_readable(self):
        rel = untung.write_data_file(self.tmp, "x", [])
        self.assertEqual(Path(self.tmp, rel).stat().st_mode & 0o777, 0o644)
        self.assertEqual(Path(self.tmp, "data").stat().st_mode & 0o777, 0o755)

    def test_no_temp_files_left_behind(self):
        untung.write_data_file(self.tmp, "x", [["a", "1", ""]])
        self.assertEqual([p.name for p in Path(self.tmp, "data").iterdir() if p.name.startswith(".tmp-")], [])


def render_report(tmp, repos, upstream_url="http://upstream.example/debian/", upstream_index=None):
    """Write the report into tmp and return the page text."""
    with contextlib.redirect_stderr(io.StringIO()):
        untung.write_html_report(repos, tmp, upstream_url, upstream_index)
    return Path(tmp, "index.html").read_text(encoding="utf-8")


def page_const(page, name):
    """Return the JSON value the page assigns to `const <name>`."""
    return json.loads(re.search(r"const %s = (.*?);\n" % name, page, re.S).group(1))


def data_rows(tmp, rel_path):
    """Rows stored in a report data file."""
    return json.loads(gzip.decompress(Path(tmp, rel_path).read_bytes()))


def repo(url="http://repo.example/blankon/", index=None, results=None, diff=None):
    return {"url": url, "index": index or {}, "results": results or [], "diff": diff or []}


class HtmlReportDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def files(self, page):
        return page_const(page, "DATA_FILES")

    def test_package_rows_store_folder_relative_to_repo(self):
        page = render_report(self.tmp, [repo(index={"dnsmasq": {
            "version": "2.93-1",
            "url": "http://repo.example/blankon/pool/main/d/dnsmasq/dnsmasq_2.93-1_all.deb",
        }})])
        self.assertEqual(page_const(page, "REPO_BASES"), ["http://repo.example/blankon/"])
        self.assertEqual(data_rows(self.tmp, self.files(page)["repos"][0]["pkg"]),
                         [["dnsmasq", "2.93-1", "pool/main/d/dnsmasq/"]])

    def test_package_outside_repo_keeps_absolute_folder(self):
        page = render_report(self.tmp, [repo(index={"odd": {
            "version": "1", "url": "http://elsewhere.example/pool/o/odd/odd_1_all.deb",
        }})])
        self.assertEqual(data_rows(self.tmp, self.files(page)["repos"][0]["pkg"]),
                         [["odd", "1", "http://elsewhere.example/pool/o/odd/"]])

    def test_upstream_rows_store_folder_relative_to_upstream(self):
        page = render_report(self.tmp, [repo()], "https://mirror.example/debian/", {"dnsmasq": {
            "version": "2.93-3",
            "url": "https://mirror.example/debian/pool/main/d/dnsmasq/dnsmasq_2.93-3_all.deb",
        }})
        self.assertEqual(page_const(page, "UPSTREAM_BASE"), "https://mirror.example/debian/")
        self.assertEqual(data_rows(self.tmp, self.files(page)["upstream"]),
                         [["dnsmasq", "2.93-3", "pool/main/d/dnsmasq/"]])

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
        self.assertEqual(data_rows(self.tmp, self.files(page)["repos"][0]["cmp"]), [
            ["dpkg", "1.23.7", "1.23.11", "b"],
            ["gone", "", "1.0-1", "r"],
            ["blankon-keyring", "2026.1", "", "n"],
            ["bash", "5.3-1", "5.3-1", "u"],
        ])

    def test_diff_rows_name_source_only_when_it_differs(self):
        page = render_report(self.tmp, [repo(diff=[
            {"package": "dnsmasq", "repo_version": "2.93-1", "repo_url": "u",
             "upstream_version": "2.93-3", "status": "behind", "source": "dnsmasq"},
            {"package": "dnsmasq-base", "repo_version": "2.93-1", "repo_url": "u",
             "upstream_version": "2.93-3", "status": "behind", "source": "dnsmasq"},
            {"package": "mutter", "repo_version": "49.1-2", "repo_url": "u",
             "upstream_version": "49.0-1", "status": "ahead", "source": "mutter"},
        ])])
        self.assertEqual(data_rows(self.tmp, self.files(page)["repos"][0]["diff"]), [
            ["dnsmasq", "2.93-1", "2.93-3", "b"],
            ["dnsmasq-base", "2.93-1", "2.93-3", "b", "dnsmasq"],
            ["mutter", "49.1-2", "49.0-1", "a"],
        ])

    def test_report_writes_matching_gzip_copy(self):
        render_report(self.tmp, [repo()])
        page = Path(self.tmp, "index.html").read_bytes()
        packed = Path(self.tmp, "index.html.gz").read_bytes()
        self.assertEqual(gzip.decompress(packed), page)

    def test_page_embeds_no_table_rows(self):
        page = render_report(self.tmp, [repo()])
        for name in ("ALL_PKG_DATA", "ALL_CMP_DATA", "ALL_DIFF_DATA", "ALL_PEER_DATA", "UPSTREAM_PKG_DATA"):
            self.assertNotIn("const %s" % name, page)

    def test_manifest_points_at_existing_files(self):
        r0 = repo()
        r0["peers"] = [{"name": "arsip-dev", "diff": []}]
        page = render_report(self.tmp, [r0])
        files = self.files(page)
        paths = [files["upstream"], *(files["repos"][0][k] for k in ("pkg", "cmp", "diff")),
                 *files["repos"][0]["peers"]]
        self.assertEqual(len(paths), 5)
        for p in paths:
            self.assertTrue(Path(self.tmp, p).is_file(), p)

    def test_slugs_are_safe_and_unique_per_table(self):
        page = render_report(self.tmp, [repo(url="file:///tmp/a b/repo/"),
                                        repo(url="http://arsip.example/"), repo(url="http://arsip.example/")])
        files = self.files(page)
        names = [Path(r[k]).name.split(".")[0] for r in files["repos"] for k in ("pkg", "cmp", "diff")]
        self.assertEqual(len(names), len(set(names)))
        for n in names:
            self.assertRegex(n, r"^[A-Za-z0-9_-]+$")

    def test_data_files_are_written_before_the_page(self):
        render_report(self.tmp, [repo()])
        page_mtime = Path(self.tmp, "index.html").stat().st_mtime_ns
        for f in Path(self.tmp, "data").iterdir():
            self.assertLessEqual(f.stat().st_mtime_ns, page_mtime)

    def test_unwritable_data_dir_leaves_old_page(self):
        Path(self.tmp, "index.html").write_text("old")
        Path(self.tmp, "data").write_text("not a dir")  # makedirs fails
        with self.assertRaises(OSError):
            render_report(self.tmp, [repo()])
        self.assertEqual(Path(self.tmp, "index.html").read_text(), "old")

    @unittest.skipUnless(shutil.which("node"), "needs node to syntax-check the page script")
    def test_page_script_is_valid_javascript(self):
        page = render_report(self.tmp, [repo()])
        script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
        js = Path(self.tmp, "page.js")
        js.write_text(script)
        self.assertEqual(subprocess.run(["node", "--check", str(js)]).returncode, 0)


class DataCleanupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        Path(self.tmp, "data").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def touch(self, name, age):
        p = Path(self.tmp, "data", name)
        p.write_bytes(b"x")
        t = 10_000_000 - age
        os.utime(p, (t, t))
        return "data/" + name

    def test_removes_only_old_unreferenced_files(self):
        old = self.touch("old.aaa.json.gz", 7200)
        kept = self.touch("kept.bbb.json.gz", 7200)
        young = self.touch("young.ccc.json.gz", 60)
        removed = untung.clean_data_dir(self.tmp, {kept}, now=10_000_000)
        self.assertEqual(removed, [old])
        self.assertTrue(Path(self.tmp, kept).exists() and Path(self.tmp, young).exists())

    def test_previous_paths_from_new_style_page(self):
        page = render_report(self.tmp, [repo()])
        self.assertEqual(untung.previous_manifest_paths(str(Path(self.tmp, "index.html"))),
                         untung.manifest_paths(page_const(page, "DATA_FILES")))

    def test_previous_paths_from_old_single_file_page(self):
        Path(self.tmp, "index.html").write_text("<script>const ALL_PKG_DATA = [];\n</script>")
        self.assertEqual(untung.previous_manifest_paths(str(Path(self.tmp, "index.html"))), set())

    def test_previous_paths_without_a_page(self):
        self.assertEqual(untung.previous_manifest_paths(str(Path(self.tmp, "index.html"))), set())

    def test_lock_file_is_never_cleaned(self):
        self.touch(".lock", 7200)
        self.assertEqual(untung.clean_data_dir(self.tmp, set(), now=10_000_000), [])

    def test_retired_file_survives_one_hour_across_runs(self):
        def a(v):
            return repo(index={"a": {"version": v, "url": "http://repo.example/blankon/pool/a/a_%s.deb" % v}})
        page = render_report(self.tmp, [a("1")])
        x = page_const(page, "DATA_FILES")["repos"][0]["pkg"]
        os.utime(Path(self.tmp, x), (1, 1))          # unchanged for ages
        render_report(self.tmp, [a("2")])             # x retired now
        render_report(self.tmp, [a("3")])             # next run: x in neither manifest
        self.assertTrue(Path(self.tmp, x).exists())   # still inside its hour
        os.utime(Path(self.tmp, x), (1, 1))          # an hour has passed
        render_report(self.tmp, [a("4")])
        self.assertFalse(Path(self.tmp, x).exists())

    def test_failed_publish_still_starts_retirement(self):
        def a(v):
            return repo(index={"a": {"version": v, "url": "http://repo.example/blankon/pool/a/a_%s.deb" % v}})
        page = render_report(self.tmp, [a("1")])
        x = page_const(page, "DATA_FILES")["repos"][0]["pkg"]
        os.utime(Path(self.tmp, x), (1, 1))          # unchanged for ages
        real = untung.write_file_atomic

        def fail_gz(path, data):
            if path.endswith(".gz") and not path.endswith(".json.gz"):
                raise OSError("disk full")
            real(path, data)
        with unittest.mock.patch.object(untung, "write_file_atomic", fail_gz):
            with self.assertRaises(OSError):
                render_report(self.tmp, [a("2")])     # page replaced, .gz copy failed
        render_report(self.tmp, [a("3")])             # next run, minutes later
        self.assertTrue(Path(self.tmp, x).exists())   # the replaced page keeps its hour

    def test_report_holds_lock_while_writing(self):
        import fcntl
        seen = []
        real = untung.write_file_atomic

        def spy(path, data):
            with open(Path(self.tmp, "data", ".lock")) as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    seen.append("free")
                except BlockingIOError:
                    seen.append("held")
            real(path, data)
        with unittest.mock.patch.object(untung, "write_file_atomic", spy):
            render_report(self.tmp, [repo()])
        self.assertTrue(seen and set(seen) == {"held"}, seen)


if __name__ == "__main__":
    unittest.main()
