"""Run every bundled suite and print one line per file.

    python tests/run_all.py                  # everything
    python tests/run_all.py realm            # only suites whose name contains "realm"
    python tests/run_all.py --jobs 8         # suites in parallel (default: min(8, cpus))
    python tests/run_all.py --timeout 300    # per-suite wall clock, seconds
    python tests/run_all.py --logs DIR       # keep every suite's full output in DIR

Python suites run under the current interpreter; the JS suites need `node` on
PATH and are reported as skipped when it is missing. `_mobile_check.py` is not
part of this set: it drives the dashboard with Playwright/Firefox and is run by
hand.

Each suite's output goes to a file rather than a pipe, so a suite that spawns the
gateway still sees a normal console and a failure can be shown with its tail.
`--logs` is what CI uses: the directory is uploaded as an artifact on failure, so
the full output of a red suite survives the run instead of having to be dug out
of a 30k-line job log.

A failure line carries the first error the suite printed, not just its last
line - the last line of a crashed suite is often a bare "Node.js v20.20.2", which
names nothing.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TAIL_LINES = 25
DEFAULT_TIMEOUT = 300
# Enough lines to catch a Python traceback or a JS "Error:" block without
# printing a whole suite log into the CI summary.
ERROR_TAIL = 12

# The first line that looks like the reason the suite stopped. Ordered loosely:
# a traceback's last line is the actual exception, so both ends are kept.
ERROR_HINTS = re.compile(
    r"(Traceback \(most recent call last\)"
    r"|^\s*[\w.]*(Error|Exception)\b"
    r"|^\s*(FAILED|AssertionError)"
    r"|is not a function"
    r"|Cannot read|Cannot find|not defined"
    r"|timed out)", re.I)


def suites(pattern):
    names = sorted(n for n in os.listdir(HERE)
                   if n.startswith("_test_") and n.endswith((".py", ".js")))
    return [n for n in names if pattern in n]


def command(name):
    path = os.path.join(HERE, name)
    if name.endswith(".js"):
        return ["node", path]
    return [sys.executable, path]


def read_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return [line.rstrip() for line in fh if line.strip()]
    except OSError:
        return []


def first_error(lines):
    """The most useful single line from a failed suite's output.

    A Python suite ends with its exception; a JS suite that dies during load ends
    with "Node.js v20.20.2" and the reason is a dozen lines above. Prefer the
    last line that looks like an error, fall back to the last line at all.
    """
    for line in reversed(lines):
        if ERROR_HINTS.search(line):
            return line.strip()
    return (lines[-1].strip() if lines else "")


def run_one(name, env, timeout, logs_dir):
    """Run one suite; return everything the caller needs to report it."""
    started = time.time()
    handle, log_path = tempfile.mkstemp(suffix=".log", prefix=name + ".")
    os.close(handle)
    timed_out = False
    try:
        with open(log_path, "w", encoding="utf-8") as sink:
            try:
                result = subprocess.run(command(name), cwd=ROOT, env=env,
                                        stdout=sink, stderr=subprocess.STDOUT,
                                        timeout=timeout)
                code = result.returncode
            except subprocess.TimeoutExpired:
                # One hung suite must not hold the whole job until its own
                # timeout: kill it and report it like any other failure.
                timed_out = True
                code = -1
        lines = read_lines(log_path)
        kept = None
        if logs_dir:
            kept = os.path.join(logs_dir, name + ".log")
            try:
                shutil.copyfile(log_path, kept)
            except OSError:
                kept = None
        return {
            "name": name,
            "ok": code == 0,
            "code": code,
            "seconds": time.time() - started,
            "timed_out": timed_out,
            "last": (lines[-1].strip() if lines else "")[:90],
            "error": first_error(lines),
            "tail": lines[-TAIL_LINES:],
            "errors_tail": lines[-ERROR_TAIL:],
            "log": kept,
        }
    finally:
        try:
            os.unlink(log_path)
        except OSError:
            pass


def parse_args(argv):
    pattern, jobs, timeout, logs_dir = "", None, DEFAULT_TIMEOUT, None
    rest = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            print(__doc__)
            return None
        if arg == "--jobs" and i + 1 < len(argv):
            jobs = int(argv[i + 1]); i += 2; continue
        if arg == "--timeout" and i + 1 < len(argv):
            timeout = int(argv[i + 1]); i += 2; continue
        if arg == "--logs" and i + 1 < len(argv):
            logs_dir = argv[i + 1]; i += 2; continue
        rest.append(arg); i += 1
    return {
        "pattern": rest[0] if rest else "",
        "jobs": jobs if jobs and jobs > 0 else min(8, os.cpu_count() or 1),
        "timeout": timeout if timeout > 0 else DEFAULT_TIMEOUT,
        "logs": logs_dir,
    }


def main(argv):
    # The parent prints each suite's last output line, and those are often
    # Chinese ("loadSettings 完整性断言通过（14 项）"). On the Windows runner
    # stdout is cp1252, so that print raised UnicodeEncodeError and killed the
    # run before it could print any summary - the child-side PYTHONIOENCODING
    # below does not cover the parent's own console. Reconfigure instead of
    # trusting the locale; errors="replace" keeps a stray character from ending
    # the run either way.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = parse_args(argv)
    if args is None:
        return 0
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [ROOT] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    # A suite may print non-ASCII (Chinese labels are common here). The child
    # writes straight into a utf-8 log file, so its own stdout encoding has to
    # be utf-8 too - on the CI Windows runner the locale is cp1252 and a
    # Chinese label would otherwise abort the suite with UnicodeEncodeError.
    env["PYTHONIOENCODING"] = "utf-8"
    have_node = shutil.which("node") is not None

    selected = suites(args["pattern"])
    if not selected:
        # A typo in the filter, or every suite deleted/renamed, would otherwise
        # report "0 passed, 0 failed" and exit 0 - the one result CI must never
        # treat as a pass.
        print("  no suite matches %r in %s" % (args["pattern"], HERE))
        return 2

    logs_dir = args["logs"]
    if logs_dir:
        try:
            os.makedirs(logs_dir, exist_ok=True)
        except OSError as exc:
            print("  cannot create %s: %s" % (logs_dir, exc))
            return 2

    passed, failed, skipped, durations = [], [], [], []
    todo = []
    for name in selected:
        if name.endswith(".js") and not have_node:
            skipped.append(name)
            print("  [skip] %-38s node is not on PATH" % name)
        else:
            todo.append(name)

    print("  running %d suite(s), jobs=%d, timeout=%ds"
          % (len(todo), args["jobs"], args["timeout"]))
    results = []
    with ThreadPoolExecutor(max_workers=args["jobs"]) as pool:
        futures = [pool.submit(run_one, name, env, args["timeout"], logs_dir)
                   for name in todo]
        for future in futures:
            results.append(future.result())
            # Report as they finish: a long run shows progress instead of going
            # quiet until the slowest suite is done.
            item = results[-1]
            durations.append((item["seconds"], item["name"]))
            if item["ok"]:
                passed.append(item["name"])
                summary = item["last"]
            else:
                failed.append(item["name"])
                summary = item["error"] or item["last"]
                if item["timed_out"]:
                    summary = "timed out after %ds" % args["timeout"]
            print("  [%s] %-38s %-52s %5.1fs"
                  % ("PASS" if item["ok"] else "FAIL", item["name"],
                     summary[:52], item["seconds"]))
            if not item["ok"]:
                if item["log"]:
                    print("        full output: %s" % item["log"])
                else:
                    print("        --- last %d lines of %s ---" % (TAIL_LINES, item["name"]))
                    for line in item["tail"]:
                        print("        " + line)

    print("")
    if durations:
        slowest = sorted(durations, reverse=True)[:3]
        print("  slowest: %s"
              % ", ".join("%s %.1fs" % (name, secs) for secs, name in slowest))
    print("  %d passed, %d failed, %d skipped  (%s)"
          % (len(passed), len(failed), len(skipped), ROOT))
    if failed:
        print("  failed: %s" % ", ".join(sorted(failed)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
