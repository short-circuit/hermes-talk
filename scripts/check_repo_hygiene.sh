#!/usr/bin/env bash
# check_repo_hygiene.sh — public-repository PII and secret gate.
#
# Scans every file that would enter the repository (tracked files plus untracked
# files that are not gitignored) for personal data, credentials and oversized
# blobs. Exits non-zero on any violation so CI and pre-commit can block the change.
#
# Rules (each rule id is named in docs/PRIVACY.md):
#   R1  personal email address
#   R2  absolute home-directory path (/home/<user>/..., /Users/<user>/...)
#   R3  hard-coded RFC 1918 (private LAN) IPv4 address
#   R4  secret-shaped token or PEM private key
#   R5  file larger than the size cap (default 2 MiB)
#   R6  committed environment file (.env and .env.*, except .env.example)
#   R7  private-suffix hostname (*.local, *.lan, *.home, *.internal)
#
# R5 size exception (documented, path-scoped):
#   android/app/src/main/assets/wakeword/*.onnx may be up to 6 MiB. The
#   sherpa-onnx KWS int8 encoder is 4.6 MiB; the model ships inside the APK and
#   recompressing it would change the pinned model. Every other path keeps the
#   default 2 MiB cap.
#
# Usage:
#   scripts/check_repo_hygiene.sh              scan the current repository
#   scripts/check_repo_hygiene.sh --self-test  prove every rule fires on fixtures
#   scripts/check_repo_hygiene.sh --help
#
# Environment:
#   HYGIENE_MAX_BYTES  size cap for R5 (default 2097152)
#
# Exit codes: 0 clean, 1 violations found, 2 usage or environment error.

set -uo pipefail

MAX_BYTES=${HYGIENE_MAX_BYTES:-2097152}
SIZE_EXCEPTION_CAP=6291456
SELF=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")

EMAIL_RE='[[:alnum:]_.%+-]+@[[:alnum:].-]+\.[[:alpha:]]{2,}'
# Addresses that are never personal: RFC 2606 example domains, GitHub noreply, git remotes.
EMAIL_ALLOW_RE='^([[:alnum:]_.%+-]+@example\.(com|org|net)|[[:alnum:]_.%+-]+@users\.noreply\.github\.com|noreply@[[:alnum:].-]+|git@github\.com)$'

HOME_RE='(/home/[[:alnum:]_][[:alnum:]_.-]*/|/Users/[[:alnum:]_][[:alnum:]_.-]*/)'
# GitHub-hosted runners run as /home/runner; that path is not personal data.
HOME_ALLOW_RE='^/home/runner/$'

LAN_RE='\b(10\.[0-9]+\.[0-9]+\.[0-9]+|192\.168\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+)\b'

SECRET_RE='(sk-[[:alnum:]_-]{20,}|gh[pousr]_[[:alnum:]]{30,}|github_pat_[[:alnum:]_]{40,}|xox[baprs]-[[:alnum:]-]{10,}|AKIA[[:alnum:]]{16}|AIza[[:alnum:]_-]{35}|hf_[[:alnum:]]{30,}|glpat-[[:alnum:]_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)'

HOST_RE='[[:alnum:]_-]+\.(local|lan|home|internal)\b'
# Synthetic hostnames are allowed: voice.example.lan, relay.example.local, ...
HOST_ALLOW_RE='^([[:alnum:]_-]+\.)*example\.(local|lan|home|internal)$'

fail=0
report() {
  # report <rule-id> <description> <hit-list>
  printf 'hygiene: %s — %s\n' "$1" "$2" >&2
  printf '%s\n' "$3" | sed 's/^/    /' >&2
  fail=1
}

# --- detectors -------------------------------------------------------------
# Each detector prints "file:line:match" for every violating token and returns
# 0 when it printed something, 1 when the input was clean.

drop_allowed() {
  # stdin: "file:line:token"; $1: anchored ERE matching a token that is allowed
  local line token
  while IFS= read -r line; do
    token=${line##*:}
    printf '%s\n' "$token" | grep -qE -- "$1" || printf '%s\n' "$line"
  done
}

r_email() { grep -HnoIE -- "$EMAIL_RE" "$@" 2>/dev/null | drop_allowed "$EMAIL_ALLOW_RE"; }
r_home() { grep -HnoIE -- "$HOME_RE" "$@" 2>/dev/null | drop_allowed "$HOME_ALLOW_RE"; }
r_lan() { grep -HnoIE -- "$LAN_RE" "$@" 2>/dev/null; }
r_secret() { grep -HnoIE -- "$SECRET_RE" "$@" 2>/dev/null; }
r_host() { grep -HnoIE -- "$HOST_RE" "$@" 2>/dev/null | drop_allowed "$HOST_ALLOW_RE"; }

size_cap_for() {
  # The only documented R5 exception: the bundled sherpa-onnx KWS models.
  case "$1" in
    android/app/src/main/assets/wakeword/*.onnx|*/android/app/src/main/assets/wakeword/*.onnx)
      printf '%s' "$SIZE_EXCEPTION_CAP"
      ;;
    *)
      printf '%s' "$MAX_BYTES"
      ;;
  esac
}

r_size() {
  local f size cap
  for f in "$@"; do
    [ -f "$f" ] || continue
    size=$(wc -c <"$f")
    cap=$(size_cap_for "$f")
    if [ "$size" -gt "$cap" ]; then
      printf '%s:0:%s bytes (cap %s)\n' "$f" "$size" "$cap"
    fi
  done
}

r_envfile() {
  local f
  for f in "$@"; do
    case "$(basename "$f")" in
      .env.example) continue ;;
      .env | .env.*) printf '%s:0:committed environment file\n' "$f" ;;
    esac
  done
}

# --- repository scan -------------------------------------------------------

collect_files() {
  local -a all=()
  local f
  mapfile -d '' -t all < <(git ls-files -z --cached --others --exclude-standard)
  for f in "${all[@]}"; do
    [ -f "$f" ] && printf '%s\0' "$f"
  done
}

scan() {
  local -a files=()
  mapfile -d '' -t files < <(collect_files)
  if [ "${#files[@]}" -eq 0 ]; then
    printf 'hygiene: no files to scan\n' >&2
    return 0
  fi

  local hits
  hits=$(r_email "${files[@]}") && [ -n "$hits" ] && report R1 "personal email address (use example.com or a GitHub noreply address)" "$hits"
  hits=$(r_home "${files[@]}") && [ -n "$hits" ] && report R2 "absolute home-directory path (use \$HOME, \${DATA_DIR} or a documented default)" "$hits"
  hits=$(r_lan "${files[@]}") && [ -n "$hits" ] && report R3 "hard-coded private LAN address (use an env var or a TEST-NET example)" "$hits"
  hits=$(r_secret "${files[@]}") && [ -n "$hits" ] && report R4 "secret-shaped token or private key (move it to .env / a CI secret)" "$hits"
  hits=$(r_size "${files[@]}") && [ -n "$hits" ] && report R5 "file over ${MAX_BYTES} bytes (keep blobs out of git or raise the cap deliberately)" "$hits"
  hits=$(r_envfile "${files[@]}") && [ -n "$hits" ] && report R6 "committed environment file (only .env.example belongs in git)" "$hits"
  hits=$(r_host "${files[@]}") && [ -n "$hits" ] && report R7 "private-suffix hostname (use 127.0.0.1, an env var or an example.* name)" "$hits"

  if [ "$fail" -eq 0 ]; then
    printf 'hygiene: clean (%d files scanned)\n' "${#files[@]}"
  else
    printf 'hygiene: FAILED — fix the items above, then re-run\n' >&2
  fi
  return "$fail"
}

# --- self-test -------------------------------------------------------------
# Proves the gate actually fails on planted data: every rule must fire on a
# violating fixture and stay silent on benign content, and a fixture repository
# must pass clean then fail once a secret is planted.

self_test() {
  local tmp rc=0
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/hygiene-selftest.XXXXXX") || {
    printf 'hygiene: cannot create temp dir\n' >&2
    return 2
  }
  trap 'rm -rf "$tmp"' RETURN

  assert_fires() { # <rule-id> <description> <content>
    local id=$1 desc=$2 f="$tmp/case-$1-$RANDOM.txt"
    printf '%s\n' "$3" >"$f"
    local out
    case "$id" in
      R1) out=$(r_email "$f") ;;
      R2) out=$(r_home "$f") ;;
      R3) out=$(r_lan "$f") ;;
      R4) out=$(r_secret "$f") ;;
      R7) out=$(r_host "$f") ;;
      *) printf 'hygiene: unknown rule %s\n' "$id" >&2; rc=1; return ;;
    esac
    if [ -z "$out" ]; then
      printf 'hygiene self-test: FAIL %s did not fire on %s\n' "$id" "$desc" >&2
      rc=1
    else
      printf 'hygiene self-test: ok   %s fired on %s (%s)\n' "$id" "$desc" "$out"
    fi
  }

  assert_silent() { # <rule-id> <description> <content>
    local id=$1 desc=$2 f="$tmp/clean-$1-$RANDOM.txt"
    printf '%s\n' "$3" >"$f"
    local out
    case "$id" in
      R1) out=$(r_email "$f") ;;
      R2) out=$(r_home "$f") ;;
      R3) out=$(r_lan "$f") ;;
      R4) out=$(r_secret "$f") ;;
      R7) out=$(r_host "$f") ;;
      *) printf 'hygiene: unknown rule %s\n' "$id" >&2; rc=1; return ;;
    esac
    if [ -n "$out" ]; then
      printf 'hygiene self-test: FAIL %s false positive on %s: %s\n' "$id" "$desc" "$out" >&2
      rc=1
    else
      printf 'hygiene self-test: ok   %s silent on %s\n' "$id" "$desc"
    fi
  }

  # Fixture values are assembled from fragments so this script does not trip
  # its own rules when the repository is scanned.
  frag() { local IFS=; printf '%s' "$*"; }
  local FIX_EMAIL FIX_PAT FIX_OPENAI FIX_AWS FIX_PEM FIX_LAN FIX_10 FIX_172 FIX_HOST FIX_HOME FIX_USERS
  FIX_EMAIL=$(frag alice @ corp-mail.net)
  FIX_PAT=$(frag ghp_ 0123456789abcdefghijklmnopqrstuvwxyzAB)
  FIX_OPENAI=$(frag sk-proj- abcdefghijklmnopqrstuvwx)
  FIX_AWS=$(frag AKIA IOSFODNN7EXAMPLE)
  FIX_PEM=$(frag '-----BEGIN OPENSSH' ' PRIVATE KEY-----')
  FIX_LAN=$(frag 192.168. 1.42)
  FIX_10=$(frag 10.0. 0.7)
  FIX_172=$(frag 172.20. 3.9)
  FIX_HOST=$(frag relay.tower .local)
  FIX_HOME=$(frag /home/ alice/)
  FIX_USERS=$(frag /Users/ alice/)

  assert_fires R1 "personal email" "contact ${FIX_EMAIL} for access"
  assert_silent R1 "example domain" 'contact noreply@example.com'
  assert_silent R1 "github noreply" 'author: 12345+octocat@users.noreply.github.com'
  assert_silent R1 "git remote" 'git clone git@github.com:owner/repo.git'

  assert_fires R2 "linux home path" "DATA_DIR=${FIX_HOME}hermes-talk/data"
  assert_fires R2 "macOS home path" "DATA_DIR=${FIX_USERS}hermes-talk/data"
  assert_silent R2 "env var" 'DATA_DIR=${HOME}/hermes-talk/data'
  assert_silent R2 "documented placeholder" 'paths look like /home/<user>/...'

  assert_fires R3 "192.168/16" "relay listening on ${FIX_LAN}:8765"
  assert_fires R3 "10/8" "inference host at ${FIX_10}"
  assert_fires R3 "172.16/12" "inference host at ${FIX_172}"
  assert_silent R3 "loopback" 'relay listening on 127.0.0.1:8765'
  assert_silent R3 "TEST-NET" 'documentation address 192.0.2.10'

  assert_fires R4 "github PAT" "GITHUB_TOKEN=${FIX_PAT}"
  assert_fires R4 "openai key" "OPENAI_API_KEY=${FIX_OPENAI}"
  assert_fires R4 "aws key" "aws_access_key_id = ${FIX_AWS}"
  assert_fires R4 "private key" "${FIX_PEM}"
  assert_silent R4 "env indirection" 'GITHUB_TOKEN=${GITHUB_TOKEN}'

  assert_fires R7 "mDNS hostname" "ssh ${FIX_HOST}"
  assert_silent R7 "synthetic hostname" 'server_name voice.example.lan;'

  # R5 and R6 need real files.
  head -c $((MAX_BYTES + 1)) /dev/zero >"$tmp/big.bin"
  if [ -n "$(r_size "$tmp/big.bin")" ]; then
    printf 'hygiene self-test: ok   R5 fired on %s bytes\n' "$((MAX_BYTES + 1))"
  else
    printf 'hygiene self-test: FAIL R5 did not fire on oversized file\n' >&2
    rc=1
  fi
  printf 'small\n' >"$tmp/small.txt"
  if [ -z "$(r_size "$tmp/small.txt")" ]; then
    printf 'hygiene self-test: ok   R5 silent on small file\n'
  else
    printf 'hygiene self-test: FAIL R5 false positive on small file\n' >&2
    rc=1
  fi

  # R5 path-scoped exception: the bundled wake-word ONNX models may reach 6 MiB.
  local exception_dir="$tmp/android/app/src/main/assets/wakeword"
  mkdir -p "$exception_dir"
  head -c $((MAX_BYTES + 1)) /dev/zero >"$exception_dir/between.onnx"
  if [ -z "$(r_size "$exception_dir/between.onnx")" ]; then
    printf 'hygiene self-test: ok   R5 exception accepts %s bytes in wakeword/*.onnx\n' "$((MAX_BYTES + 1))"
  else
    printf 'hygiene self-test: FAIL R5 exception rejected a file between the default and exception caps\n' >&2
    rc=1
  fi
  head -c $((SIZE_EXCEPTION_CAP + 1)) /dev/zero >"$exception_dir/over.onnx"
  if [ -n "$(r_size "$exception_dir/over.onnx")" ]; then
    printf 'hygiene self-test: ok   R5 exception rejects %s bytes in wakeword/*.onnx\n' "$((SIZE_EXCEPTION_CAP + 1))"
  else
    printf 'hygiene self-test: FAIL R5 exception accepted a file over the exception cap\n' >&2
    rc=1
  fi
  head -c $((MAX_BYTES + 1)) /dev/zero >"$tmp/outside-exception.bin"
  if [ -n "$(r_size "$tmp/outside-exception.bin")" ]; then
    printf 'hygiene self-test: ok   R5 still rejects %s bytes outside the exception\n' "$((MAX_BYTES + 1))"
  else
    printf 'hygiene self-test: FAIL R5 accepted an oversized file outside the exception\n' >&2
    rc=1
  fi
  printf 'API_KEY=x\n' >"$tmp/.env"
  if [ -n "$(r_envfile "$tmp/.env")" ]; then
    printf 'hygiene self-test: ok   R6 fired on .env\n'
  else
    printf 'hygiene self-test: FAIL R6 did not fire on .env\n' >&2
    rc=1
  fi
  printf 'API_KEY=placeholder\n' >"$tmp/.env.example"
  if [ -z "$(r_envfile "$tmp/.env.example")" ]; then
    printf 'hygiene self-test: ok   R6 silent on .env.example\n'
  else
    printf 'hygiene self-test: FAIL R6 false positive on .env.example\n' >&2
    rc=1
  fi

  # End-to-end: a clean fixture repository passes, a planted secret fails.
  local repo="$tmp/repo"
  mkdir -p "$repo/docs"
  git -C "$repo" init -q
  printf 'Relay listens on 127.0.0.1 and the docs use noreply@example.com.\n' >"$repo/README.md"
  printf 'See /home/<user>/... placeholders and 192.0.2.10 examples.\n' >"$repo/docs/NOTES.md"
  if (cd "$repo" && bash "$SELF" >/dev/null 2>&1); then
    printf 'hygiene self-test: ok   clean fixture repository passes\n'
  else
    printf 'hygiene self-test: FAIL clean fixture repository was rejected\n' >&2
    (cd "$repo" && bash "$SELF") >&2 || true
    rc=1
  fi
  printf 'GITHUB_TOKEN=%s\n' "$FIX_PAT" >"$repo/docs/leak.md"
  if (cd "$repo" && bash "$SELF" >/dev/null 2>&1); then
    printf 'hygiene self-test: FAIL planted secret in fixture repository was accepted\n' >&2
    rc=1
  else
    printf 'hygiene self-test: ok   planted secret in fixture repository is rejected\n'
  fi

  if [ "$rc" -eq 0 ]; then
    printf 'hygiene self-test: PASS\n'
  else
    printf 'hygiene self-test: FAILED\n' >&2
  fi
  return "$rc"
}

case "${1:-}" in
  --self-test) self_test ;;
  --help | -h)
    sed -n '2,45p' "$SELF" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  "")
    git rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
      printf 'hygiene: not inside a git work tree\n' >&2
      exit 2
    }
    scan
    exit $?
    ;;
  *)
    printf 'hygiene: unknown argument %s (try --help)\n' "$1" >&2
    exit 2
    ;;
esac
