#!/usr/bin/env bash
# Fails when the repository contains personal data, secrets, or oversized files.
set -euo pipefail

fail=0
note() { printf 'hygiene: %s\n' "$*" >&2; fail=1; }

# 1. Personal email addresses (allow example.com and GitHub noreply)
if git grep -nEI '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' -- . \
   | grep -vE '(@example\.(com|org)|@users\.noreply\.github\.com|noreply@)' ; then
  note "personal email address found"
fi

# 2. Home-directory paths
if git grep -nE '/home/[a-z0-9_-]+/' -- . ; then
  note "absolute home path found (use \$HOME or \${DATA_DIR})"
fi

# 3. Hard-coded private LAN addresses (127.0.0.1 is allowed)
if git grep -nE '\b(10\.[0-9]+\.[0-9]+\.[0-9]+|192\.168\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+)\b' -- . ; then
  note "hard-coded LAN address found (use an env var or TEST-NET example)"
fi

# 4. Obvious secrets
if git grep -nE '(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{10,})' -- . ; then
  note "secret-looking token found"
fi

# 5. Oversized files
while IFS= read -r f; do
  size=$(wc -c <"$f")
  if [ "$size" -gt 2097152 ]; then note "file over 2 MB: $f"; fi
done < <(git ls-files)

exit "$fail"
