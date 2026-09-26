"""Identify the runtime inside a released portable package, then build one.

The release zip ships an embedded CPython under `python/`, but nothing in this
repository produces that zip, so nobody can tell which distribution the runtime
came from or rebuild the artifact from a tag. This script answers both:

1. `--probe` compares the runtime inside an official release against a few
   candidate distributions (python-build-standalone, python.org embeddable,
   the python.org NuGet package) and reports which one matches file-for-file.
   Matching means: every file the official package has is present in the
   candidate with the same size.
2. With the winner (or an explicit `--runtime`) it assembles a package from the
   repository's tracked files and reports the size and SHA256. Development-only
   paths (tests/, .github/, .gitignore, _fixbats.py) are left out: the gateway
   never imports anything from tests/, and shipping them only bloats the
   archive.

Usage:
    python tools/package_probe.py --official <zip path or URL> --out build
    python tools/package_probe.py --official ... --out build --runtime <dir>
"""
import argparse
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile

# Candidate runtimes, newest first. `prefix` is the archive-level directory that
# maps onto the package's `python/` directory.
CANDIDATES = [
    ("python-build-standalone 20260924",
     "https://github.com/astral-sh/python-build-standalone/releases/download/"
     "20260924/cpython-3.12.14+20260924-x86_64-pc-windows-msvc-install_only.tar.gz",
     "tar.gz", ""),
    ("python-build-standalone 20260901",
     "https://github.com/astral-sh/python-build-standalone/releases/download/"
     "20260901/cpython-3.12.14+20260901-x86_64-pc-windows-msvc-install_only.tar.gz",
     "tar.gz", ""),
    ("python.org embeddable 3.12.14",
     "https://www.python.org/ftp/python/3.12.14/python-3.12.14-embed-amd64.zip",
     "zip", ""),
    ("python.org NuGet python 3.12.14",
     "https://www.nuget.org/api/v2/package/python/3.12.14",
     "zip", "tools/"),
]

# Never shipped to end users: the gateway does not import any of these.
DEV_PATHS = ("tests/", ".github/", ".gitignore", "_fixbats.py")
DEV_FILES = (".gitignore", "_fixbats.py")


def log(message):
    print(message, flush=True)


def download(url, target, retries=3):
    if os.path.exists(target) and os.path.getsize(target) > 0:
        log("  cached  %s" % os.path.basename(target))
        return target
    last = None
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "package-probe"})
            with urllib.request.urlopen(request, timeout=120) as response, \
                    open(target, "wb") as sink:
                shutil.copyfileobj(response, sink)
            log("  fetched %s (%.1f MB)"
                % (os.path.basename(target), os.path.getsize(target) / 1048576))
            return target
        except Exception as exc:            # noqa: BLE001 - report and retry
            last = exc
            log("  attempt %d failed: %s" % (attempt, exc))
            time.sleep(2 * attempt)
    raise RuntimeError("could not download %s: %s" % (url, last))


def archive_members(path, kind, prefix):
    """Return {relative name: size} for the runtime part of an archive."""
    out = {}
    if kind == "tar.gz":
        with tarfile.open(path, "r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                name = member.name
                if "/" in name:
                    name = name.split("/", 1)[1]      # drop the archive root
                out[name] = member.size
    else:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                if prefix and name.startswith(prefix):
                    name = name[len(prefix):]
                out[name] = info.file_size
    return out


def extract(path, kind, prefix, target):
    if os.path.isdir(target):
        shutil.rmtree(target)
    os.makedirs(target)
    if kind == "tar.gz":
        with tarfile.open(path, "r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                name = member.name.split("/", 1)[1] if "/" in member.name else member.name
                dest = os.path.join(target, name.replace("/", os.sep))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with tar.extractfile(member) as src, open(dest, "wb") as sink:
                    shutil.copyfileobj(src, sink)
    else:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                if prefix and name.startswith(prefix):
                    name = name[len(prefix):]
                dest = os.path.join(target, name.replace("/", os.sep))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with archive.open(info) as src, open(dest, "wb") as sink:
                    shutil.copyfileobj(src, sink)


def official_runtime(official_zip, work):
    if official_zip.startswith("http"):
        official_zip = download(official_zip, os.path.join(work, "official.zip"))
    with zipfile.ZipFile(official_zip) as archive:
        manifest = {}
        for info in archive.infolist():
            if info.is_dir() or not info.filename.startswith("wb-proxy/python/"):
                continue
            manifest[info.filename[len("wb-proxy/python/"):]] = info.file_size
    return manifest


def compare(official, candidate):
    """matched / size mismatch / missing, judged from the official side."""
    matched = [n for n in official if n in candidate and candidate[n] == official[n]]
    mismatched = [n for n in official if n in candidate and candidate[n] != official[n]]
    missing = [n for n in official if n not in candidate]
    return matched, mismatched, missing


def tracked_files(repo):
    listing = subprocess.run(["git", "-C", repo, "ls-files"], check=True,
                             capture_output=True, text=True).stdout
    names = []
    for name in listing.splitlines():
        if not name or name.endswith(".zip"):
            continue
        if name.startswith(DEV_PATHS) or name in DEV_FILES:
            continue
        names.append(name)
    return sorted(names)


def build(repo, runtime_dir, out_zip):
    names = tracked_files(repo)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.write(os.path.join(repo, name.replace("/", os.sep)),
                          "wb-proxy/" + name)
        for root, _dirs, files in os.walk(runtime_dir):
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, runtime_dir).replace(os.sep, "/")
                archive.write(full, "wb-proxy/python/" + rel)
    with open(out_zip, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    return names, digest


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--official", required=True,
                        help="official release zip (path or URL) to probe against")
    parser.add_argument("--out", default="build", help="working directory")
    parser.add_argument("--repo", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        help="repository root (default: the parent of this file)")
    parser.add_argument("--runtime", default=None,
                        help="use this runtime directory instead of probing candidates")
    parser.add_argument("--skip-probe", action="store_true")
    args = parser.parse_args(argv[1:])

    work = os.path.abspath(args.out)
    os.makedirs(work, exist_ok=True)

    log("official package: %s" % args.official)
    official = official_runtime(args.official, work)
    log("  runtime files in the official package: %d (%.1f MB)"
        % (len(official), sum(official.values()) / 1048576))
    log("")

    best = None
    runtime_dir = args.runtime
    if not args.skip_probe:
        log("probing candidate runtimes")
        for label, url, kind, prefix in CANDIDATES:
            log("- %s" % label)
            try:
                path = download(url, os.path.join(work, url.rsplit("/", 1)[-1]))
                members = archive_members(path, kind, prefix)
                matched, mismatched, missing = compare(official, members)
                score = len(matched) / max(1, len(official))
                log("  files=%d  matched=%d  size-mismatch=%d  missing=%d  score=%.1f%%"
                    % (len(members), len(matched), len(mismatched), len(missing), score * 100))
                for name in (mismatched + missing)[:6]:
                    log("    diff: %s" % name)
                if best is None or score > best[0]:
                    best = (score, label, path, kind, prefix)
            except Exception as exc:            # noqa: BLE001 - keep probing others
                log("  unusable: %s" % exc)
        if best and best[0] > 0.99:
            log("")
            log("=> runtime identified: %s (%.1f%% of the official files match byte size)"
                % (best[1], best[0] * 100))
            runtime_dir = os.path.join(work, "runtime")
            extract(best[2], best[3], best[4], runtime_dir)
        elif best:
            log("")
            log("=> no exact match; closest is %s at %.1f%% - the package below is"
                " built from it as a demo, not as a reproduction" % (best[1], best[0] * 100))
            runtime_dir = os.path.join(work, "runtime")
            extract(best[2], best[3], best[4], runtime_dir)

    if not runtime_dir or not os.path.isdir(runtime_dir):
        log("")
        log("no runtime to build with; stopping after the probe")
        return 0

    out_zip = os.path.join(work, "wb-proxy-demo.zip")
    names, digest = build(args.repo, runtime_dir, out_zip)
    log("")
    log("built %s" % out_zip)
    log("  repository files : %d (dev-only paths excluded: %s)"
        % (len(names), ", ".join(DEV_PATHS + DEV_FILES)))
    log("  size             : %.1f MB" % (os.path.getsize(out_zip) / 1048576))
    log("  sha256           : %s" % digest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
