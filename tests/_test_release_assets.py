"""Release assets are built from an explicit list, not from the checkout.

Issue #28 turns a `v*` tag into a portable ZIP, two OpenWrt packages and a
SHA256SUMS file, and hands #29 an explicit list of the files a release owns.
These assertions pin that contract locally, so a release cannot ship a ZIP that
silently picked up whatever happened to be in the working tree (or dropped a
newly added wb_*.py module), and the maintainer's release notes cannot be
overwritten by the generated download block.

Run with: python tests/_test_release_assets.py
"""
import glob
import hashlib
import os
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "release"))

import release_tools as tools  # noqa: E402


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class ManifestTests(unittest.TestCase):
    def test_every_listed_path_exists(self):
        for name in tools.read_manifest():
            self.assertTrue(os.path.isfile(os.path.join(ROOT, name)),
                            "清单里的 %s 不存在" % name)

    def test_every_module_is_listed(self):
        # The regression this list exists for: a new wb_*.py that nobody
        # remembered to add would otherwise be missing from the release, and
        # the launcher would fail with ModuleNotFoundError on a user's machine.
        on_disk = sorted(os.path.basename(p) for p in glob.glob(os.path.join(ROOT, "wb_*.py")))
        self.assertTrue(on_disk, "仓库里没有 wb_*.py？")
        listed = set(tools.read_manifest())
        self.assertEqual([n for n in on_disk if n not in listed], [])

    def test_launchers_and_dashboard_are_listed(self):
        listed = tools.read_manifest()
        for name in ("dashboard.html", "wb_proxy.py", "README.md", "LICENSE",
                     "start-wb-proxy.bat", "start-wb-proxy.sh",
                     "start-wb-proxy.command", "start-wb-proxy-lan.sh"):
            self.assertIn(name, listed)

    def test_entries_are_relative_and_inside_the_repo(self):
        for name in tools.read_manifest():
            self.assertFalse(os.path.isabs(name), name)
            self.assertNotIn("..", name.split("/"), name)
            self.assertNotIn("\\", name, name)

    def test_no_duplicates(self):
        listed = tools.read_manifest()
        self.assertEqual(len(listed), len(set(listed)))

    def test_user_data_directories_are_not_managed_files(self):
        # #29 replaces exactly the managed files and must leave accounts/ and
        # usage/ byte-for-byte alone, so no manifest entry may live inside them.
        for name in tools.read_manifest():
            for protected in tools.PROTECTED:
                self.assertFalse(name.startswith(protected),
                                 "%s 落在受保护目录 %s 里" % (name, protected))

    def test_development_only_files_stay_out(self):
        listed = tools.read_manifest()
        for name in ("Dockerfile", "docker-compose.yml", "quick-deploy.sh",
                     "tests/run_all.py"):
            self.assertNotIn(name, listed)


class PortableZipTests(unittest.TestCase):
    def build(self, directory, name="out.zip"):
        return tools.build_portable("1.6.17", "v1.6.17",
                                    os.path.join(directory, name), source=ROOT)

    def test_archive_holds_exactly_the_manifest_plus_the_marker(self):
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            path, files = self.build(directory)
            with zipfile.ZipFile(path) as zf:
                self.assertEqual(sorted(zf.namelist()),
                                 sorted(files + [tools.MANIFEST_NAME]))

    def test_embedded_manifest_describes_the_release(self):
        import json
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            path, files = self.build(directory)
            with zipfile.ZipFile(path) as zf:
                manifest = json.loads(zf.read(tools.MANIFEST_NAME).decode("utf-8"))
            self.assertEqual(manifest["files"], files)
            self.assertEqual(manifest["protected"], tools.PROTECTED)
            self.assertEqual(manifest["version"], tools.source_version(ROOT))
            self.assertEqual(manifest["tag"], "v1.6.17")

    def test_archive_paths_are_safe(self):
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            path, _ = self.build(directory)
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    self.assertFalse(name.startswith("/"), name)
                    self.assertNotIn("..", name.split("/"), name)

    def test_rebuild_is_byte_identical(self):
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            first, _ = self.build(directory, "a.zip")
            second, _ = self.build(directory, "b.zip")
            with open(first, "rb") as fa, open(second, "rb") as fb:
                self.assertEqual(fa.read(), fb.read())

    def test_version_mismatch_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            with self.assertRaises(SystemExit):
                tools.build_portable("9.9.9", "v9.9.9",
                                     os.path.join(directory, "x.zip"), source=ROOT)


class ChecksumTests(unittest.TestCase):
    def test_covers_every_asset_once_in_sha256sum_format(self):
        with tempfile.TemporaryDirectory(prefix="relsum-") as directory:
            names = ["workbuddy2api-hub-v1.6.17.zip",
                     "workbuddy2api_1.6.17-1_all.ipk",
                     "workbuddy2api-1.6.17-r1.apk"]
            assets = []
            for i, name in enumerate(names):
                p = os.path.join(directory, name)
                with open(p, "wb") as fh:
                    fh.write(b"asset-%d" % i)
                assets.append(p)
            out = os.path.join(directory, "SHA256SUMS")
            lines = tools.write_checksums(assets, out)
            self.assertEqual(len(lines), len(names))
            for line, path in zip(lines, sorted(assets, key=os.path.basename)):
                digest, name = line.split("  ")
                self.assertEqual(len(digest), 64)
                self.assertEqual(name.strip(), os.path.basename(path))
                with open(path, "rb") as fh:
                    self.assertEqual(digest, hashlib.sha256(fh.read()).hexdigest())

    def test_hash_follows_the_bytes(self):
        with tempfile.TemporaryDirectory(prefix="relsum-") as directory:
            p = os.path.join(directory, "a.zip")
            with open(p, "wb") as fh:
                fh.write(b"one")
            before = tools.sha256(p)
            with open(p, "wb") as fh:
                fh.write(b"two")
            self.assertNotEqual(before, tools.sha256(p))


class ReleaseBodyTests(unittest.TestCase):
    block = None

    def setUp(self):
        names = tools.asset_names("1.6.17", "1")
        self.block = tools.download_block("1.6.17", *names)

    def test_first_compose_has_the_marker_and_the_fixed_block(self):
        body = tools.compose_body("", self.block)
        self.assertIn(tools.MARKER, body)
        for expected in ("## 下载说明", "workbuddy2api-hub-v1.6.17.zip",
                         "workbuddy2api_1.6.17-1_all.ipk",
                         "workbuddy2api-1.6.17-r1.apk", "SHA256SUMS"):
            self.assertIn(expected, body)

    def test_maintainer_notes_above_the_marker_survive(self):
        notes = "## 本版要点\n\n- 修了一个真问题\n"
        body = tools.compose_body(notes + "\n" + tools.MARKER + "\n旧块\n", self.block)
        self.assertIn("- 修了一个真问题", body)
        self.assertNotIn("旧块", body)

    def test_recompose_is_idempotent(self):
        once = tools.compose_body("", self.block)
        twice = tools.compose_body(once, self.block)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count(tools.MARKER), 1)


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = read(".github", "workflows", "release.yml")

    def test_triggers_on_version_tags(self):
        self.assertIn('tags: ["v*"]', self.text)

    def test_release_is_created_as_a_draft_and_never_published(self):
        self.assertIn("--draft", self.text)
        self.assertNotIn("--draft=false", self.text)
        # Every release mutation must stay on the draft side: `gh release edit`
        # without --draft, or `gh release create` without it, publishes.
        for line in self.text.splitlines():
            stripped = line.strip()
            if stripped.startswith("gh release edit") or stripped.startswith("gh release create"):
                self.assertIn("--draft", stripped, stripped)

    def test_tag_must_match_the_source_version(self):
        # The gate reads both source strings, compares the tag against them and
        # cross-checks the OpenWrt Makefile, so a tag cannot ship a package
        # whose version disagrees with the code inside it.
        self.assertIn("github.ref_name", self.text)
        self.assertIn("server_version", self.text)
        self.assertIn('"version"', self.text)
        self.assertIn("TAGVER", self.text)
        self.assertIn("wrt/openwrt/workbuddy2api/Makefile", self.text)

    def test_uses_the_manifest_tooling(self):
        self.assertIn("release/release_tools.py", self.text)
        self.assertIn("SHA256SUMS", self.text)

    def test_the_apk_metadata_is_checked_against_the_tag(self):
        # "non-empty and metadata/version agree with the tag" is an explicit
        # acceptance criterion, so the workflow proves it with the SDK's own
        # apk rather than trusting a file name.
        self.assertIn("adbdump", self.text)
        self.assertIn("apk-dump.txt", self.text)
        self.assertIn("python3-light", self.text)

    def test_packages_are_staged_flat_before_upload(self):
        # The rehearsal caught this: download-artifact with merge-multiple
        # rebuilds the tree relative to the upload root, so uploading
        # wrt/ipk/*.ipk and wrt/apk/*.apk in one artifact delivered them as
        # dist/ipk/ and dist/apk/ - the checksum step's dist/*.apk glob then
        # matched nothing and the release job died on a FileNotFoundError.
        # Every artifact is uploaded from one flat directory instead.
        self.assertIn("merge-multiple: true", self.text)
        self.assertIn("cp wrt/ipk/*.ipk wrt/apk/*.apk dist/", self.text)
        for line in self.text.splitlines():
            stripped = line.strip()
            if stripped.startswith("wrt/") and (".ipk" in stripped or ".apk" in stripped):
                self.fail("上传路径不能直接指向 wrt/：%s" % stripped)


if __name__ == "__main__":
    unittest.main()
