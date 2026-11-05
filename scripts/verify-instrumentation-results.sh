#!/usr/bin/env bash
# verify-instrumentation-results.sh — fail unless the named instrumentation
# cases ran and passed, and nothing was skipped.
#
# A zero Gradle exit status is not enough evidence that a gate ran: a class
# filter that matches nothing, an APK that failed to load, or an `@Ignore`d test
# can all leave the build green while the promised coverage never happened. This
# script reads the JUnit XML that Gradle/UTP wrote and fails unless:
#   * every `--require` case is present and passed,
#   * no case failed or errored,
#   * no case was skipped (a skip is not a pass),
#   * at least one case ran.
#
# Usage:
#   scripts/verify-instrumentation-results.sh --dir <results-dir> \
#       --require <package.Class#method> [--require <package.Class#method> ...]
#
# Exit codes: 0 verified, 1 gate failed, 2 usage or environment error.
set -euo pipefail

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
fi

python3 - "$@" <<'PY'
"""Read JUnit XML from an Android instrumentation run and enforce the gate."""

import argparse
import os
import sys
import xml.etree.ElementTree as ET

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--dir", required=True)
parser.add_argument("--require", action="append", default=[])
args, unknown = parser.parse_known_args()
if unknown:
    print(f"verify: unknown argument(s): {' '.join(unknown)}", file=sys.stderr)
    sys.exit(2)

if not os.path.isdir(args.dir):
    print(f"verify: results directory {args.dir} does not exist", file=sys.stderr)
    sys.exit(2)

files = []
for root, _dirs, names in os.walk(args.dir):
    for name in names:
        if name.startswith("TEST-") and name.endswith(".xml"):
            files.append(os.path.join(root, name))

if not files:
    print(f"verify: no JUnit XML under {args.dir}; the suite did not run", file=sys.stderr)
    sys.exit(1)

tests = 0
failures = 0
errors = 0
skipped = 0
cases = {}

for path in sorted(files):
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        print(f"verify: cannot parse {path}: {exc}", file=sys.stderr)
        sys.exit(1)
    for testcase in tree.iter("testcase"):
        classname = testcase.get("classname") or ""
        name = testcase.get("name") or ""
        case_id = f"{classname}#{name}"
        tests += 1
        if testcase.find("skipped") is not None:
            skipped += 1
            cases[case_id] = "skipped"
        elif testcase.find("failure") is not None or testcase.find("error") is not None:
            if testcase.find("failure") is not None:
                failures += 1
                cases[case_id] = "failed"
            else:
                errors += 1
                cases[case_id] = "errored"
        else:
            cases[case_id] = "passed"

print(
    f"verify: {tests} test(s) in {len(files)} XML file(s): "
    f"{failures} failure(s), {errors} error(s), {skipped} skipped"
)

problems = []
if failures:
    problems.append(f"{failures} failure(s)")
if errors:
    problems.append(f"{errors} error(s)")
if skipped:
    problems.append(f"{skipped} skipped test(s) — a skip is not a pass")

for required in args.require:
    outcome = cases.get(required)
    if outcome is None:
        problems.append(f"required case did not run: {required}")
    elif outcome != "passed":
        problems.append(f"required case {outcome}: {required}")
    else:
        print(f"verify: required case passed: {required}")

if tests == 0:
    problems.append("no test case executed")

if problems:
    print("verify: FAILED", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    sys.exit(1)

print("verify: PASSED — every required case ran and passed, nothing was skipped")
PY
