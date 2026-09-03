#!/usr/bin/env python3
"""Assert the ntfy grants still match what ADR-0033 declares.

Why this is checked rather than trusted
---------------------------------------
The whole safety property of the taxonomy is that `nas-arr` — the token stored
inside three \\*arr SQLite databases and bind-mounted into three containers —
**cannot publish to `nas-critical`**. That is enforced by one `ntfy access`
grant and nothing else. A grant is a single command away from being widened,
`ntfy access nas-arr 'nas-*' rw` looks tidier than three explicit lines, and
nothing about a wrong grant is visible until the day it matters.

So this asserts the shape:

* `nas-scripts` — write-only across `nas-*`
* `nas-arr` — write-only on exactly `nas-media`, `nas-attention`, `nas-requests`
* `nas-phone` — read-only across `nas-*`

**Write-only, not read-write**, deliberately: ADR-0012 makes the publisher
accounts unable to read the topics, so a leak of this box's `.env` exposes no
alert history. Nothing in the design reads from a publisher account.

It parses `ntfy access` rather than probing with a publish, because probing
would put a message in every lane on every run — the monitor becoming the noise.
`make notify-test` does the empirical version, on demand.

Exit codes
----------
  0  the grants match
  1  they do not (or `ntfy access` could not be read)
  2  fatal: no ntfy container to ask

Usage
-----
  scripts/check-ntfy-acls.py            # assert
  scripts/check-ntfy-acls.py --print    # show the grants, secrets redacted
"""

from __future__ import annotations

import re
import subprocess
import sys

CONTAINER = "ntfy"

# user -> (mode, topics). A frozenset means "exactly these"; a string means one
# wildcard pattern.
EXPECTED: dict[str, tuple[str, frozenset[str] | str]] = {
  "nas-scripts": ("write-only", "nas-*"),
  "nas-arr": ("write-only", frozenset({"nas-media", "nas-attention", "nas-requests"})),
  "nas-phone": ("read-only", "nas-*"),
}

USER_LINE = re.compile(r"^user (\S+) \(role: (\S+?),")
GRANT_LINE = re.compile(r"^- (read-write|read-only|write-only|denied) access to topic (\S+)")


def read_access() -> str | None:
  """`ntfy access` output, or None if the container cannot be reached."""
  try:
    proc = subprocess.run(
      ["docker", "exec", CONTAINER, "ntfy", "access"],
      capture_output=True, text=True, timeout=30, check=False,
    )
  except (OSError, subprocess.TimeoutExpired):
    return None
  return proc.stdout if proc.returncode == 0 else None


def parse_access(text: str) -> dict[str, dict[str, str]]:
  """`ntfy access` output -> {user: {topic: mode}}. Pure."""
  out: dict[str, dict[str, str]] = {}
  current = ""
  for raw in text.splitlines():
    line = raw.strip()
    match = USER_LINE.match(line)
    if match:
      current = match.group(1)
      out.setdefault(current, {})
      continue
    grant = GRANT_LINE.match(line)
    if grant and current:
      out[current][grant.group(2)] = grant.group(1)
  return out


def check(parsed: dict[str, dict[str, str]]) -> list[str]:
  """Findings; empty means the grants match. Pure."""
  findings: list[str] = []
  for user, (mode, topics) in sorted(EXPECTED.items()):
    grants = parsed.get(user)
    if grants is None:
      findings.append(f"{user}: no such ntfy user")
      continue
    if isinstance(topics, str):
      actual = grants.get(topics)
      if actual != mode:
        findings.append(
          f"{user}: expected {mode} on {topics!r}, found {actual or 'no grant'}"
        )
      extra = sorted(set(grants) - {topics})
      if extra:
        findings.append(f"{user}: unexpected extra grant(s) {extra}")
      continue
    wrong_mode = sorted(t for t in topics if grants.get(t) != mode)
    if wrong_mode:
      findings.append(
        f"{user}: expected {mode} on {wrong_mode}, found "
        f"{ {t: grants.get(t, 'none') for t in wrong_mode} }"
      )
    extra = sorted(set(grants) - set(topics))
    if extra:
      findings.append(
        f"{user}: has {extra} beyond its three lanes. This token lives in *arr "
        f"SQLite; widening it is how a compromised *arr reaches nas-critical."
      )
  return findings


def redact(text: str) -> str:
  return re.sub(r"(tk_[A-Za-z0-9]{4})[A-Za-z0-9]+", r"\1<redacted>", text)


def main(argv: list[str] | None = None) -> int:
  args = argv if argv is not None else sys.argv[1:]
  text = read_access()
  if text is None:
    print(f"    !!! cannot run `ntfy access` in the {CONTAINER} container", file=sys.stderr)
    return 2

  if "--print" in args:
    print(redact(text).rstrip())
    tokens = subprocess.run(
      ["docker", "exec", CONTAINER, "ntfy", "token", "list"],
      capture_output=True, text=True, timeout=30, check=False,
    )
    print()
    print(redact(tokens.stdout).rstrip() or "(no tokens)")
    return 0

  findings = check(parse_access(text))
  if findings:
    for finding in findings:
      print(f"    !!! {finding}")
    print("        ADR-0033 declares the grants; `make notify-acl` prints the live ones.")
    return 1
  print("    ok: nas-scripts wo nas-*, nas-arr wo on 3 lanes only, nas-phone ro nas-*")
  return 0


if __name__ == "__main__":
  sys.exit(main())
