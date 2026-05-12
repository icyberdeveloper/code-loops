#!/usr/bin/env bash
# Pre-publish guard: fail CI if private/personal information creeps into
# tracked files. Catches the universal categories of leakage that any
# public repo should be free of, regardless of what the maintainer's
# private projects look like.
#
# Run locally: bash scripts/check_no_leakage.sh

set -euo pipefail

# Files/paths to skip entirely (this script itself, lockfile, license).
EXCLUDE_FILES='^(\.gitignore|uv\.lock|LICENSE|scripts/check_no_leakage\.sh)$'

# === Universal leakage categories ===
#
# 1. Cyrillic characters — agent prompts and docs must be English so
#    English-only contributors aren't shut out. (Specific bilingual
#    markers like `_POSTMORTEM_MARKERS` are intentional and live in
#    src/code_loops/cli.py + tech_writer.py — those files are excluded
#    via WHITELIST.)
# 2. Personal home paths — /home/<user>/ and /Users/<user>/ should never
#    be hardcoded; use placeholders or env-resolved paths.
# 3. Common secret prefixes — Anthropic, OpenAI, GitHub, Slack tokens.
# 4. Email addresses other than noreply@anthropic.com (used in
#    Co-Authored-By trailers).

CYRILLIC='[А-Яа-яЁё]'
PERSONAL_PATHS='/home/[a-z][a-z0-9_-]+/|/Users/[A-Za-z][A-Za-z0-9_-]+/'
SECRET_PREFIXES='sk-ant-[a-zA-Z0-9_-]{20,}|sk-proj-[a-zA-Z0-9_-]{20,}|xox[bpoa]-[a-zA-Z0-9-]+|ghp_[a-zA-Z0-9]{30,}|gho_[a-zA-Z0-9]{30,}|AIza[0-9A-Za-z_-]{30,}'
EMAIL='[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'

# Files where bilingual support strings are intentional (Russian markers
# enable Russian-language postmortem detection).
BILINGUAL_FILES='^(src/code_loops/cli\.py|src/code_loops/stages/tech_writer\.py|README\.md)$'

# Whitelist for legitimate matches. Each pattern is ERE matched against
# `file:lineno:content` from grep.
WHITELIST='noreply@anthropic\.com'                    # Co-Authored-By trailer
WHITELIST+='|/home/<user>/|/Users/<user>/'            # placeholder text in docs
WHITELIST+='|/home/<your[- ]user>/|~/<your[- ]dir>/'  # placeholder text variants

leakage=""

scan() {
  local label="$1" pattern="$2" exclude_extra="${3:-}"
  while IFS= read -r f; do
    [ -f "$f" ] || continue
    if [ -n "$exclude_extra" ] && [[ "$f" =~ $exclude_extra ]]; then
      continue
    fi
    matches=$(grep -nE "$pattern" "$f" 2>/dev/null | grep -vE "$WHITELIST" || true)
    if [ -n "$matches" ]; then
      leakage+="--- [$label] $f ---\n$matches\n"
    fi
  done < <(git ls-files | grep -vE "$EXCLUDE_FILES")
}

scan "cyrillic"     "$CYRILLIC"          "$BILINGUAL_FILES"
scan "personal-path" "$PERSONAL_PATHS"
scan "secret"       "$SECRET_PREFIXES"
scan "email"        "$EMAIL"

if [ -n "$leakage" ]; then
  echo "FAIL: leakage detected:" >&2
  printf "%b" "$leakage" >&2
  echo "" >&2
  echo "Either remove the leak, or — if the match is legitimate (e.g. a" >&2
  echo "placeholder example, an intentional bilingual marker) — adjust" >&2
  echo "WHITELIST or BILINGUAL_FILES in scripts/check_no_leakage.sh." >&2
  exit 1
fi

echo "OK: no leakage detected."
