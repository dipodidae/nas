#!/usr/bin/env bash
# Fail if any real secret from .env is in a tracked or staged file. Part of
# `make check`, so the pre-commit hook runs it on every commit.
#
# Why: this repo is PUBLIC. On 2026-09-27 a real, reused password turned up in
# scripts/legacy/INSTALLATION_COMPLETE.txt. It had been committed on
# 2026-01-22, and the 2026-09-02 "remove hardcoded credentials" pass missed
# that file. The value only ever lived in .env, so .env is the list to check
# against: no pattern-matching guesswork.
#
# Also WARNS when a secret in .env is still the placeholder from .env.example
# (slskd's web password was literally `change-this-password`). That is a
# publicly known credential, not a leak, so it warns rather than fails.
#
# Prints key NAMES only, never values. Exit 0 clean (or no .env, e.g. in CI),
# 1 a secret is committed.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
[ -f .env ] || { echo "    skip: no .env here (CI) -- nothing to compare against"; exit 0; }

SECRET_KEY_RE='(PASS|PASSWORD|SECRET|TOKEN|API_KEY|_KEY$|HASH|PRIVATE)'
# Values that are placeholders or not secret at all, never worth matching.
SKIP_VALUE_RE='^(/.*|~.*|\$\{.*|your[-_].*|change[-_]this.*|changeme|example.*|placeholder.*|true|false|[0-9]{1,6})$'

PLACEHOLDER_RE='^(your[-_].*|change[-_]this.*|changeme|example.*|placeholder.*)$'

value_of() { sed -n "s/^$1=//p" "$2" 2>/dev/null | tail -1 | sed -e "s/^'//" -e "s/'$//" -e 's/^"//' -e 's/"$//'; }

rc=0
leaked=() defaults=()
while IFS= read -r key; do
  val="$(value_of "$key" .env)"
  [ "${#val}" -ge 8 ] || continue
  if [[ "$val" =~ $SKIP_VALUE_RE ]]; then
    # Only a placeholder-shaped value that matches the example is a default
    # credential; a path that matches its example is just a path.
    [[ "$val" =~ $PLACEHOLDER_RE ]] && [ "$val" = "$(value_of "$key" .env.example)" ] && defaults+=("$key")
    continue
  fi
  # Tracked files in the worktree, plus whatever is staged for this commit.
  if git grep -qF -e "$val" -- . ':!.env' 2>/dev/null || git grep --cached -qF -e "$val" -- . ':!.env' 2>/dev/null; then
    leaked+=("$key")
  fi
done < <(grep -oE '^[A-Z0-9_]+=' .env | tr -d '=' | grep -E "$SECRET_KEY_RE" | sort -u)

if [ ${#leaked[@]} -gt 0 ]; then
  echo "    !!! the real value of ${leaked[*]} is in a tracked file. This repo is PUBLIC:" >&2
  echo "        remove it, then ROTATE it -- git history keeps it after the file is fixed." >&2
  echo "        Find it with: git grep -nF \"\$(sed -n 's/^KEY=//p' .env)\"" >&2
  rc=1
fi
if [ ${#defaults[@]} -gt 0 ]; then
  echo "    WARN still the .env.example placeholder (a publicly known credential): ${defaults[*]}" >&2
fi
[ $rc = 0 ] && echo "    ok: no .env secret is in a tracked or staged file"
exit $rc
