#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Release asset tooling (issue #28).

Four subcommands, all standard-library only so the release workflow needs no
third-party action to produce the assets:

    files                      print the managed portable file list
    portable                   build the portable ZIP (+ release-manifest.json)
    checksums                  write SHA256SUMS over the final assets
    body                       compose the Draft Release body (fixed block)

The portable ZIP is built from ``release/portable.txt`` rather than from a
repository-wide glob: that list is also the contract #29's updater consumes
("replace exactly these files, delete exactly these stale managed files"), so
an accidental new file in the checkout can never end up in a release.

    python release/release_tools.py portable --version 1.6.17 --tag v1.6.17 \
        --out dist/workbuddy2api-hub-v1.6.17.zip
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

MANIFEST = os.path.join(HERE, "portable.txt")
MANIFEST_NAME = "release-manifest.json"
MARKER = "<!-- release-download-block -->"
SCHEMA = 1

# Paths the updater must never touch. They travel with the archive's manifest
# so #29 can assert the contract instead of hardcoding it a second time.
PROTECTED = ["accounts/", "usage/"]

# Reproducible-build timestamp, the same convention the .ipk recipe uses.
DEFAULT_EPOCH = 1700000000


def read_manifest(path=MANIFEST):
    """Manifest paths, in file order, comments and blanks dropped."""
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
    return out


def source_version(source=ROOT):
    """The version the code itself claims, from wb_proxy.py's two strings."""
    import re
    with open(os.path.join(source, "wb_proxy.py"), encoding="utf-8") as fh:
        text = fh.read()
    server = re.search(r'server_version\s*=\s*"wb-proxy/([^"]+)"', text)
    if not server:
        raise SystemExit("wb_proxy.py 里找不到 server_version")
    return server.group(1)


def _zip_date(epoch):
    import time
    t = time.gmtime(max(epoch, 315532800))  # 1980-01-01 is ZIP's floor
    return (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)


def build_portable(version, tag, out_path, source=ROOT, epoch=DEFAULT_EPOCH):
    """Write the portable ZIP and return (path, [managed files])."""
    got = source_version(source)
    if got != version:
        raise SystemExit("版本不一致：wb_proxy.py=%s，参数=%s" % (got, version))

    files = read_manifest()
    missing = [p for p in files if not os.path.isfile(os.path.join(source, p))]
    if missing:
        raise SystemExit("清单里的文件不存在：%s" % ", ".join(missing))

    manifest = {
        "schema": SCHEMA,
        "name": "workbuddy2api-hub",
        "version": version,
        "tag": tag,
        "files": files,
        "protected": PROTECTED,
    }
    blob = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    when = _zip_date(epoch)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Sorted so the same checkout gives a byte-identical archive.
        for name in sorted(files) + [MANIFEST_NAME]:
            if name == MANIFEST_NAME:
                data = blob.encode("utf-8")
            else:
                with open(os.path.join(source, name), "rb") as fh:
                    data = fh.read()
            info = zipfile.ZipInfo(name, date_time=when)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix, so the mode above survives
            zf.writestr(info, data)
    return out_path, files


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_checksums(assets, out_path):
    """`sha256sum` format over the final uploaded assets, sorted by name."""
    lines = []
    for path in sorted(assets, key=lambda p: os.path.basename(p)):
        lines.append("%s  %s\n" % (sha256(path), os.path.basename(path)))
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.writelines(lines)
    return lines


def download_block(version, zip_name, ipk_name, apk_name):
    """The stable help section the issue pins. Keep it short and unchanged."""
    return "\n".join([
        "## 下载说明",
        "",
        "- `%s`" % zip_name,
        "  - 通用便携版（Python 源码 + 看板 + 启动脚本）",
        "  - Windows / macOS / Linux",
        "  - 需要 Python 3.9+",
        "  - 解压后使用对应平台的启动脚本",
        "",
        "- `%s`" % ipk_name,
        "  - OpenWrt 的 opkg 系安装包",
        "  - 适用于对应受支持的旧版 OpenWrt 路线",
        "",
        "- `%s`" % apk_name,
        "  - OpenWrt 25.x+ 的 apk-tools 安装包",
        "",
        "- `SHA256SUMS`",
        "  - Release 资产的 SHA-256 校验值",
        "",
    ])


def compose_body(existing, block):
    """Everything the maintainer wrote above the marker, then the fresh block.

    Re-running is idempotent: the second pass finds the same text above the
    marker, so an updated asset list never duplicates the section or eats the
    maintainer's notes.
    """
    above = ""
    if existing:
        above = existing.split(MARKER, 1)[0].rstrip()
    if not above:
        above = "<!-- 版本说明写在这里；下面的「下载说明」由 release 工作流维护 -->"
    return above + "\n\n" + MARKER + "\n" + block


def asset_names(version, release):
    return (
        "workbuddy2api-hub-v%s.zip" % version,
        "workbuddy2api_%s-%s_all.ipk" % (version, release),
        "workbuddy2api-%s-r%s.apk" % (version, release),
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("files", help="print the managed portable file list")

    p = sub.add_parser("portable", help="build the portable ZIP")
    p.add_argument("--version", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--source", default=ROOT)
    p.add_argument("--epoch", type=int, default=int(os.environ.get("SOURCE_DATE_EPOCH", DEFAULT_EPOCH)))

    c = sub.add_parser("checksums", help="write SHA256SUMS")
    c.add_argument("--out", required=True)
    c.add_argument("assets", nargs="+")

    b = sub.add_parser("body", help="compose the Draft Release body")
    b.add_argument("--version", required=True)
    b.add_argument("--release", default="1")
    b.add_argument("--existing", default=None, help="file with the current body ('-' for stdin)")
    b.add_argument("--out", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "files":
        for name in read_manifest():
            print(name)
        return 0

    if args.cmd == "portable":
        path, files = build_portable(args.version, args.tag, args.out,
                                     source=args.source, epoch=args.epoch)
        print("portable: %s（%d 个受管文件 + %s）" % (path, len(files), MANIFEST_NAME))
        print("sha256  : %s" % sha256(path))
        return 0

    if args.cmd == "checksums":
        lines = write_checksums(args.assets, args.out)
        sys.stdout.writelines(lines)
        return 0

    if args.cmd == "body":
        existing = ""
        if args.existing == "-":
            existing = sys.stdin.read()
        elif args.existing and os.path.isfile(args.existing):
            with open(args.existing, encoding="utf-8") as fh:
                existing = fh.read()
        zip_name, ipk_name, apk_name = asset_names(args.version, args.release)
        text = compose_body(existing, download_block(args.version, zip_name, ipk_name, apk_name))
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print("body    : %s（%d 字节）" % (args.out, len(text.encode("utf-8"))))
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
