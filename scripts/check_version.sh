#!/usr/bin/env bash
# Asserts that every shipped version string agrees.
#
# Usage:
#   scripts/check_version.sh              # sources must agree with each other
#   scripts/check_version.sh 0.1.0        # ... and with the expected release version
#
# Reads the three sources of truth: the Python package metadata, the runtime
# `__version__`, and the Android versionName. Exits non-zero on any mismatch.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

fail=0

pyproject=$(sed -n 's/^version = "\(.*\)"$/\1/p' relay/pyproject.toml | head -1)
runtime=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' relay/__init__.py | head -1)
android=$(sed -n 's/^[[:space:]]*versionName = "\(.*\)"$/\1/p' android/app/build.gradle.kts | head -1)

printf 'relay/pyproject.toml: %s\n' "${pyproject:-<missing>}"
printf 'relay/__init__.py:     %s\n' "${runtime:-<missing>}"
printf 'android versionName:   %s\n' "${android:-<missing>}"

for name in pyproject runtime android; do
    value=${!name}
    if [ -z "$value" ]; then
        echo "check_version: no version found for $name" >&2
        fail=1
    fi
done

if [ "$fail" -eq 0 ] && { [ "$pyproject" != "$runtime" ] || [ "$pyproject" != "$android" ]; }; then
    echo "check_version: version strings disagree" >&2
    fail=1
fi

if [ "$#" -ge 1 ]; then
    expected=$1
    if [ "$fail" -eq 0 ] && [ "$pyproject" != "$expected" ]; then
        echo "check_version: expected $expected, sources say $pyproject" >&2
        fail=1
    fi
    printf 'expected:              %s\n' "$expected"
fi

if [ "$fail" -ne 0 ]; then
    exit 1
fi

echo "check_version: ok ($pyproject)"
