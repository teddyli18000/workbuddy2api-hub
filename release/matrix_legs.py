#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The test-matrix legs a release has to wait for (issue #28).

A release may only be written when the *whole* existing matrix passed on the
target commit - Ubuntu 3.9, Ubuntu 3.12 and Windows 3.12 today. Trusting the
tests workflow's overall conclusion is not enough: it stays green if a leg is
dropped from the matrix, and that would quietly weaken the release gate.

So the legs are read from `.github/workflows/tests.yml` itself and printed one
job name per line, in the form the Actions API reports them
(`<os> / python <version>`). Adding a leg there widens the gate automatically.

    python release/matrix_legs.py

Exit status: 0 with at least one leg printed, 1 when the file cannot be parsed
(a gate that cannot name the legs must not pass).
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TESTS_YML = os.path.join(ROOT, ".github", "workflows", "tests.yml")

# tests.yml names each job `${{ matrix.os }} / python ${{ matrix.python }}`.
JOB_NAME = "%s / python %s"


def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_include(text):
    """[(key, value), ...] entries of the first `matrix.include` list."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*include:\s*(#.*)?$", line):
            start = i + 1
            break
    if start is None:
        return []

    entries = []
    current = None
    block_indent = None
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if block_indent is None:
            if not stripped.startswith("-"):
                return []          # `include:` is not a list after all
            block_indent = indent
        elif indent < block_indent:
            break                  # the list ended
        if stripped.startswith("-"):
            current = {}
            entries.append(current)
            stripped = stripped[1:].strip()
        if not stripped or current is None:
            continue
        key, _, value = stripped.partition(":")
        if not _:
            continue
        current[key.strip()] = _unquote(value)
    return entries


def legs(text=None):
    """Job names of every matrix leg, in file order."""
    if text is None:
        with open(TESTS_YML, encoding="utf-8") as fh:
            text = fh.read()
    out = []
    for entry in parse_include(text):
        os_name, python = entry.get("os"), entry.get("python")
        if os_name and python:
            out.append(JOB_NAME % (os_name, python))
    return out


def main():
    names = legs()
    if not names:
        print("无法从 .github/workflows/tests.yml 解析出测试矩阵——"
              "发布门禁必须能点名每一条腿，拒绝继续", file=sys.stderr)
        return 1
    for name in names:
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
