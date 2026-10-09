"""Release assets are built from an explicit list, not from the checkout.

Issue #28 turns a `v*` tag into a portable ZIP, two OpenWrt packages and a
SHA256SUMS file, and hands #29 an explicit list of the files a release owns.
These assertions pin that contract locally, so a release cannot ship a ZIP that
silently picked up whatever happened to be in the working tree (or dropped a
newly added wb_*.py module), and the maintainer's release notes cannot be
overwritten by the generated download block.

Two of them exist because a review round found the release doing the wrong
thing:

  * the portable asset has to be the green package - the trimmed CPython the
    launchers start as `python\\python.exe` - not a source-only archive that
    happens to share the name;
  * nothing may write a Draft Release on the strength of a reduced test run:
    every release mutation needs the whole matrix, and the legs are read from
    tests.yml so a leg dropped there cannot silently weaken the gate.

Run with: python tests/_test_release_assets.py
"""
import ast
import glob
import hashlib
import os
import re
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "release"))

import matrix_legs  # noqa: E402
import portable_runtime  # noqa: E402
import release_tools as tools  # noqa: E402

# The legs the repository has to keep running for a release today. The gate
# reads them from tests.yml rather than from this tuple - this is the
# expectation the reader checks that derivation against.
EXPECTED_LEGS = (
    "ubuntu-latest / python 3.9",
    "ubuntu-latest / python 3.12",
    "windows-latest / python 3.12",
)


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def make_runtime(directory, extra=()):
    """A stand-in runtime that satisfies portable_runtime.verify()."""
    for rel in list(portable_runtime.REQUIRED_FILES) + list(extra):
        path = os.path.join(directory, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"stub:" + rel.encode("utf-8"))
    return directory


# ---- "only a tag push may write a release" --------------------------------
#
# A release mutation is a command that is actually run: either a line that
# starts with it (inside a `run: |` block) or a one-line `run:` step. The
# preview job is allowed to *print* the same commands inside an echo, so
# matching the text anywhere would flag the wrong job - anchoring on the
# command position is what separates "runs it" from "talks about it".
MUTATION_LINE = re.compile(
    r"^\s*(?:"
    r"gh\s+(?:release\s+(?:create|edit|upload|delete)"
    r"|api\b[^\n]*(?:-X|--method)\s*(?:PATCH|POST|PUT|DELETE))"
    r"|run:\s*gh\s+(?:release\s+(?:create|edit|upload|delete)|api\b)"
    r")",
    re.MULTILINE,
)
# The one guard that makes a job unreachable from a manual run: a dispatch has
# neither this event nor a tag ref, so no input combination can satisfy it.
TAG_PUSH_GUARD = "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"


def workflow_jobs(text):
    """{job id: job text} for the top-level `jobs:` mapping of a workflow."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == "jobs:":
            start = i
            break
    if start is None:
        return {}
    jobs, current, buf = {}, None, []
    for line in lines[start + 1:]:
        if not line.strip():
            if current:
                buf.append(line)
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            break
        if indent == 2 and not line.lstrip().startswith("#") and line.rstrip().endswith(":"):
            if current:
                jobs[current] = "\n".join(buf)
            current = line.strip()[:-1]
            buf = []
            continue
        if current is not None:
            buf.append(line)
    if current:
        jobs[current] = "\n".join(buf)
    return jobs


def job_guard(job_text):
    """The job-level `if:` value, or '' when the job has none."""
    header = []
    for line in job_text.splitlines():
        if line.strip() == "steps:":
            break
        header.append(line)
    match = re.search(r"(?m)^\s*if:\s*(.+?)\s*$", "\n".join(header))
    return match.group(1) if match else ""


def dispatch_input_names(text):
    """Input names declared under `on.workflow_dispatch.inputs`."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "workflow_dispatch:":
            continue
        parent = len(line) - len(line.lstrip())
        for j in range(i + 1, len(lines)):
            cur = lines[j]
            if not cur.strip() or cur.lstrip().startswith("#"):
                continue
            indent = len(cur) - len(cur.lstrip())
            if indent <= parent:
                break
            if cur.strip() != "inputs:":
                continue
            names = []
            for nxt in lines[j + 1:]:
                if not nxt.strip() or nxt.lstrip().startswith("#"):
                    continue
                nind = len(nxt) - len(nxt.lstrip())
                if nind <= indent:
                    break
                if nind == indent + 2 and nxt.strip().endswith(":"):
                    names.append(nxt.strip()[:-1])
            return names
    return []


def release_writer_jobs(text):
    """Jobs that run at least one release-mutating command."""
    return sorted(name for name, body in workflow_jobs(text).items()
                  if MUTATION_LINE.search(body))


def check_release_writers(text):
    """Raise AssertionError unless only a tag push can reach a release writer."""
    writers = release_writer_jobs(text)
    assert writers, "没找到任何 release 写入点——检查器本身失效了，不是工作流变干净了"
    for name in writers:
        guard = job_guard(workflow_jobs(text)[name])
        assert guard == TAG_PUSH_GUARD, (
            "job %s 会写 release，但它的 if: 不是 tag 推送（实际 %r）" % (name, guard))
    inputs = dispatch_input_names(text)
    assert inputs == ["tag"], (
        "workflow_dispatch 只能有 tag 一个输入，实际是 %s" % inputs)


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

    def test_runtime_data_files_are_listed(self):
        # wb_pricing._candidate_file() prefers pricing/pricing.json over the
        # copy inlined in the module, so a package without it silently ships
        # older prices than the release it came from.
        self.assertIn("pricing/pricing.json", tools.read_manifest())

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


class RuntimeTrimTests(unittest.TestCase):
    """The trim rules reproduce the runtime the existing package ships."""

    def test_the_app_modules_survive_the_trim(self):
        # Every stdlib module the gateway imports has to be in the contract
        # list, or the trim could drop the thing it depends on.
        imported = set()
        for path in glob.glob(os.path.join(ROOT, "wb_*.py")):
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imported.add(node.module.split(".")[0])
        local = {os.path.basename(p)[:-3] for p in glob.glob(os.path.join(ROOT, "wb_*.py"))}
        # sys / time and friends are compiled into python312.dll, so the trim
        # cannot remove them and they have no Lib/ file to check.
        needed = sorted(m for m in imported
                        if m not in local and m not in sys.builtin_module_names)
        self.assertTrue(needed)
        covered = set(portable_runtime.REQUIRED_FILES)
        missing = [m for m in needed
                   if ("Lib/%s.py" % m) not in covered and ("Lib/%s/__init__.py" % m) not in covered]
        self.assertEqual(missing, [], "这些导入没有出现在运行时常量表里：%s" % missing)

    def test_the_documented_trim_is_exactly_what_goes(self):
        for rel in ("Lib/multiprocessing", "Lib/tkinter/__init__.py", "Lib/idlelib/idle.py",
                    "Lib/site-packages/pip/__init__.py", "Lib/venv/__init__.py",
                    "Scripts/pip.exe", "include/Python.h", "libs/python312.lib",
                    "tcl/tk8.6/init.tcl", "pythonw.exe", "python312.pdb",
                    "DLLs/_tkinter.pyd", "DLLs/tcl86t.dll", "DLLs/tk86t.dll",
                    "DLLs/zlib1.dll", "DLLs/_testcapi.pyd", "Lib/__pycache__/os.cpython-312.pyc"):
            self.assertFalse(portable_runtime.keep(rel), "%s 应该被裁掉" % rel)

    def test_what_the_launchers_need_survives(self):
        for rel in ("python.exe", "python3.dll", "python312.dll", "vcruntime140.dll",
                    "LICENSE.txt", "Lib/os.py", "Lib/ssl.py", "Lib/sqlite3/__init__.py",
                    "DLLs/_socket.pyd", "DLLs/_ssl.pyd", "DLLs/libssl-3-x64.dll",
                    "DLLs/_ctypes.pyd", "DLLs/libffi-8.dll"):
            self.assertTrue(portable_runtime.keep(rel), "%s 不能被裁掉" % rel)

    def test_verify_rejects_a_runtime_that_cannot_start_the_gateway(self):
        with tempfile.TemporaryDirectory(prefix="relrt-") as directory:
            make_runtime(directory)
            portable_runtime.verify(directory)          # the fixture is valid

            os.remove(os.path.join(directory, "python.exe"))
            with self.assertRaises(SystemExit):
                portable_runtime.verify(directory)

    def test_verify_rejects_trimmed_files_coming_back(self):
        with tempfile.TemporaryDirectory(prefix="relrt-") as directory:
            make_runtime(directory)
            os.makedirs(os.path.join(directory, "Lib", "multiprocessing"))
            with self.assertRaises(SystemExit):
                portable_runtime.verify(directory)

    def test_verify_rejects_stray_debug_symbols(self):
        with tempfile.TemporaryDirectory(prefix="relrt-") as directory:
            make_runtime(directory, extra=["DLLs/_socket.pdb"])
            with self.assertRaises(SystemExit):
                portable_runtime.verify(directory)


class PortableZipTests(unittest.TestCase):
    def build(self, directory, name="out.zip", runtime=None, **kwargs):
        runtime = runtime or make_runtime(os.path.join(directory, "python"))
        return tools.build_portable("1.6.17", "v1.6.17",
                                    os.path.join(directory, name),
                                    source=ROOT, runtime_dir=runtime, **kwargs)

    def test_archive_holds_the_manifest_the_runtime_and_the_marker(self):
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            path, files = self.build(directory)
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
            root = tools.PACKAGE_ROOT
            self.assertEqual(sorted(n for n in names if not n.startswith(root + "/")),
                             [tools.MANIFEST_NAME])
            self.assertEqual(sorted(n[len(root) + 1:] for n in names if n.startswith(root + "/")
                                    and not n.startswith(root + "/python/")),
                             sorted(files))
            self.assertIn("%s/python/python.exe" % root, names)
            self.assertIn("%s/python/Lib/os.py" % root, names)

    def test_the_archive_is_not_a_source_archive(self):
        # The whole point of the round-1 fix: a ZIP without the runtime is a
        # different, much less useful asset, so the builder must not produce it.
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            path, _ = self.build(directory)
            runtime_files = [n for n in zipfile.ZipFile(path).namelist()
                             if n.endswith("python/python.exe")]
            self.assertEqual(len(runtime_files), 1)

    def test_a_missing_runtime_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            with self.assertRaises(SystemExit):
                tools.build_portable("1.6.17", "v1.6.17",
                                     os.path.join(directory, "x.zip"), source=ROOT,
                                     runtime_dir=None)
            with self.assertRaises(SystemExit):
                tools.build_portable("1.6.17", "v1.6.17",
                                     os.path.join(directory, "x.zip"), source=ROOT,
                                     runtime_dir=os.path.join(directory, "nope"))

    def test_a_runtime_that_fails_the_contract_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            bad = make_runtime(os.path.join(directory, "bad"))
            os.remove(os.path.join(bad, "python.exe"))
            with self.assertRaises(SystemExit):
                tools.build_portable("1.6.17", "v1.6.17",
                                     os.path.join(directory, "x.zip"), source=ROOT,
                                     runtime_dir=bad)

    def test_embedded_manifest_describes_the_release(self):
        import json
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            path, files = self.build(directory)
            with zipfile.ZipFile(path) as zf:
                manifest = json.loads(zf.read(tools.MANIFEST_NAME).decode("utf-8"))
            self.assertEqual(manifest["files"], files)
            self.assertEqual(manifest["protected"], tools.PROTECTED)
            self.assertEqual(manifest["root"], tools.PACKAGE_ROOT)
            self.assertEqual(manifest["version"], tools.source_version(ROOT))
            self.assertEqual(manifest["tag"], "v1.6.17")
            # #29 replaces the runtime as one subtree, so the manifest has to
            # name it instead of leaving it implicit.
            self.assertEqual(manifest["runtime"]["prefix"], "python/")
            self.assertEqual(manifest["runtime"]["python"], portable_runtime.RUNTIME_VERSION)
            self.assertGreater(manifest["runtime"]["files"], 0)

    def test_the_launchers_look_where_the_runtime_is(self):
        # The packaged path and the path the launcher probes have to agree, or
        # the green package silently falls back to a system interpreter.
        bat = read("start-wb-proxy.bat")
        self.assertIn("python\\python.exe", bat)
        sh = read("start-wb-proxy.sh")
        self.assertIn("python/bin/python3", sh)

    def test_archive_paths_are_safe(self):
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            path, _ = self.build(directory)
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    self.assertFalse(name.startswith("/"), name)
                    self.assertNotIn("..", name.split("/"), name)

    def test_rebuild_is_byte_identical(self):
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            runtime = make_runtime(os.path.join(directory, "python"))
            first, _ = self.build(directory, "a.zip", runtime=runtime)
            second, _ = self.build(directory, "b.zip", runtime=runtime)
            with open(first, "rb") as fa, open(second, "rb") as fb:
                self.assertEqual(fa.read(), fb.read())

    def test_version_mismatch_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            runtime = make_runtime(os.path.join(directory, "python"))
            with self.assertRaises(SystemExit):
                tools.build_portable("9.9.9", "v9.9.9",
                                     os.path.join(directory, "x.zip"), source=ROOT,
                                     runtime_dir=runtime)


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


class MatrixLegsTests(unittest.TestCase):
    """The release gate names every leg instead of trusting a summary."""

    def test_the_repository_matrix_is_the_expected_three(self):
        self.assertEqual(tuple(matrix_legs.legs()), EXPECTED_LEGS)

    def test_dropping_a_leg_narrows_what_the_gate_requires(self):
        # Deriving the legs from tests.yml is the point: if a leg disappears
        # there, the gate stops waiting for it - and the unit above is what
        # makes that a deliberate change rather than a silent weakening.
        text = read(".github", "workflows", "tests.yml")
        without = text.replace('          - os: windows-latest\n            python: "3.12"\n', "")
        self.assertNotEqual(without, text, "tests.yml 里没有找到预期的 windows 腿")
        self.assertEqual(tuple(matrix_legs.legs(without)), EXPECTED_LEGS[:2])

    def test_an_unparseable_matrix_yields_nothing(self):
        # Empty means "the gate must refuse", never "no legs to check".
        self.assertEqual(matrix_legs.legs("name: tests\njobs:\n  test:\n    runs-on: x\n"), [])


class ReleaseToolingTests(unittest.TestCase):
    def test_the_tools_survive_a_non_utf8_console(self):
        # The Windows runner's stdout is cp1252, and the first Chinese progress
        # line aborted the first rehearsal of this workflow - the repository
        # already paid for the same lesson in tests/run_all.py. Both tools pin
        # their own streams instead of trusting the console.
        for name in ("portable_runtime.py", "release_tools.py"):
            text = read("release", name)
            self.assertIn('reconfigure(encoding="utf-8"', text, name)

    def test_the_workflow_pins_utf8_for_every_python_process(self):
        self.assertIn('PYTHONUTF8: "1"', read(".github", "workflows", "release.yml"))


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

    def test_the_portable_asset_is_built_and_booted_on_windows(self):
        # A Linux job can only assemble a source archive; the green package has
        # to be produced where its own runtime can be executed.
        portable = self.text.split("  portable:", 1)[1].split("\n  openwrt:", 1)[0]
        self.assertIn("runs-on: windows-latest", portable)
        self.assertIn("release/portable_runtime.py", portable)
        self.assertIn("--runtime python", portable)
        self.assertIn("python\\python.exe", portable)
        self.assertIn("/health", portable)
        self.assertIn("RUNTIME_SHA256", portable)
        self.assertIn("release-manifest.json", portable)

    def test_every_release_writer_needs_the_full_matrix(self):
        # The gate derives the legs from tests.yml and checks each one by name.
        self.assertIn("release/matrix_legs.py", self.text)
        self.assertIn("matrix=full", self.text)
        self.assertIn("needs.gate.outputs.matrix", self.text)
        self.assertIn("writes == 'true'", self.text)
        # The authorisation has to come from the step that checks the legs; an
        # output wired to the wrong step is empty, and the writer then refuses
        # the mutation (which is how the first run caught this).
        gate = self.text.split("  gate:", 1)[1].split("\n  portable:", 1)[0]
        self.assertIn("matrix: ${{ steps.matrix.outputs.matrix }}", gate)
        self.assertIn("id: matrix", gate)

    # ---- only a tag push may write a release -------------------------------
    #
    # A manual run builds and hashes exactly what a tag push would, then stops.
    # The writer is a separate job whose `if:` a dispatch cannot satisfy, and
    # the checks below fail if that ever stops being true.

    def test_only_a_tag_push_can_reach_the_release_writer(self):
        check_release_writers(self.text)
        self.assertEqual(release_writer_jobs(self.text), ["write-draft"])

    def test_the_writer_guard_is_the_tag_push_condition(self):
        jobs = workflow_jobs(self.text)
        self.assertEqual(job_guard(jobs["write-draft"]), TAG_PUSH_GUARD)
        # The preview job runs for both events, so it must stay read-only.
        self.assertEqual(release_writer_jobs(self.text).count("draft-preview"), 0)
        self.assertIn("gh release view", jobs["draft-preview"])

    def test_dispatch_declares_no_input_that_could_authorise_a_write(self):
        self.assertEqual(dispatch_input_names(self.text), ["tag"])
        self.assertNotIn("inputs.dry_run", self.text)
        self.assertNotIn("dry_run", self.text)

    def test_the_checker_rejects_a_writer_moved_onto_the_dispatch_path(self):
        # Negative evidence, kept in the suite: a checker that cannot fail
        # proves nothing. Moving the writer onto the dispatch path has to be
        # caught, or the checks above are decoration.
        mutated = self.text.replace(TAG_PUSH_GUARD,
                                    "github.event_name == 'workflow_dispatch'")
        self.assertNotEqual(mutated, self.text)
        with self.assertRaises(AssertionError):
            check_release_writers(mutated)

    def test_the_checker_rejects_a_mutation_hidden_in_another_job(self):
        mutated = self.text.replace(
            "      - name: Show what a tag push would do\n",
            "      - name: sneak a write in\n        run: |\n"
            "          gh release delete \"$TAG\" --yes\n"
            "      - name: Show what a tag push would do\n", 1)
        self.assertNotEqual(mutated, self.text)
        with self.assertRaises(AssertionError):
            check_release_writers(mutated)

    def test_the_checker_rejects_a_one_line_run_step(self):
        # The short form is just as much a writer as the block form.
        mutated = self.text.replace(
            "      - name: Show what a tag push would do\n",
            "      - name: sneak a write in\n        run: gh release delete \"$TAG\" --yes\n"
            "      - name: Show what a tag push would do\n", 1)
        self.assertNotEqual(mutated, self.text)
        with self.assertRaises(AssertionError):
            check_release_writers(mutated)

    def test_the_checker_rejects_a_dispatch_input_that_could_gate_a_write(self):
        mutated = self.text.replace(
            "    inputs:\n      tag:\n",
            "    inputs:\n      tag:\n      dry_run:\n        description: \"x\"\n", 1)
        self.assertNotEqual(mutated, self.text)
        with self.assertRaises(AssertionError):
            check_release_writers(mutated)

    def test_the_checker_notices_when_it_finds_no_writer_at_all(self):
        # If every writer disappears the checker must complain rather than pass
        # vacuously.
        mutated = MUTATION_LINE.sub("true ", self.text)
        self.assertNotEqual(mutated, self.text)
        with self.assertRaises(AssertionError):
            check_release_writers(mutated)

    def test_a_reduced_run_only_ever_packages(self):
        # The single-OS suite may run for a packaging-only dry run and nowhere
        # else: that is what keeps it from authorising a release mutation.
        marker = "Run every suite (packaging-only dry run"
        self.assertIn(marker, self.text)
        block = self.text.split(marker, 1)[1].split("run:", 1)[0]
        self.assertIn("steps.decide.outputs.writes != 'true'", block)

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
