#!/usr/bin/env python3
"""Update this stack's services: check, back up, apply, then prove it took.

This is the `nas-service-upgrades` workflow as a script. Nothing here is a
shortcut around that skill -- it is the same sequence, with the parts that
were easy to skip by hand made unskippable.

Why this exists rather than `docker compose pull && up -d`
----------------------------------------------------------
Three things went wrong doing this by hand on 2026-09-15, and each is a
guard in here now:

* **diun missed a pinned-tag update for two days.** qbittorrent
  `5.2.3_v2.0.14-ls476` shipped 2026-09-13; diun's own stored view still
  named `ls475` as newest on the 15th. A notifier that silently stops
  covering something is the ADR-0024 failure shape, so this script does its
  own tag check (`rank_key`) and never trusts diun as the source of truth.
* **A green healthcheck is not a working service.** Every failure in
  ADR-0035, ADR-0036 and ADR-0039 was invisible to health, so nothing here
  is judged by health alone: after `up -d` it asks the CONTAINER what tag it
  is running, and a Postgres server what version it is. That check caught
  this script's own worst bug -- it pulled the target image but never edited
  the pinned tag in the compose file, so `up -d` recreated from the OLD tag
  and it reported "applied" while changing nothing.
* **CAP_KILL is not a clean stop (ADR-0041).** Jellyfin's WAL was still
  dirty after a "successful" stop, which silently staled every backup that
  copied the `.db` alone. `sqlite_backup` stops first and *asserts* the
  `-wal`/`-shm` files are gone before it calls the copy good.

Postgres majors are handled in full: dump with the newer client, prove the
dump, stop dependants, rename PGDATA aside (never delete -- that rename is
what makes it revertible), move the bind mount up for pg18+, initdb, restore
before dependants return, and verify. Credentials come from the resolved
compose model; an earlier version refused the whole operation for want of
credentials that were sitting in the model all along.

Posture
-------
It **applies by default**: run it bare and it updates, backs up and verifies
end to end. `--dry-run` prints the plan and touches nothing.

That is a deliberate departure from ADR-0025, which retired Watchtower so
that no unattended thing could stop/remove/create a container. The
difference this script has to earn: Watchtower's recreate was non-atomic and
unverified, and it left qbittorrent with **no container at all** for 13h.
This one takes a proven backup first, applies one service at a time, and
halts the whole run on the first failed verification rather than moving on.
It does **not** roll back -- reverting a tag into a store that has already
migrated one way is what made an incident worse on 2026-09-10 (ADR-0036).

Exit codes
----------
  0  everything checked/applied and verified
  1  partial -- something was skipped or is behind, nothing is broken
  2  fatal -- a verification failed, a backup could not be proved, or the
     compose model is unreadable

Usage
-----
  python scripts/stack_update.py                     # check, apply, verify
  python scripts/stack_update.py --dry-run           # print the plan only
  python scripts/stack_update.py --check             # report what is behind, apply nothing
  python scripts/stack_update.py --service jellyfin  # one service
  python scripts/stack_update.py --kind drift        # only floating-tag services
  python scripts/stack_update.py --no-notify         # do not publish to ntfy
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Which subtrees regenerate is one fact, so it is imported rather than
# restated. It matters more here than it does there: config_backup runs
# against a LIVE stack, while this copy runs with the service STOPPED, so
# every byte of it is downtime. Unfiltered, jellyfin's tree is 197,841 files /
# 21.9 GB -- ~17 minutes cross-device -- of which 19 GB is `data/metadata`
# artwork Jellyfin re-fetches on demand, and lidarr's is 13.3 GB with 8.1 GB
# of MediaCover. Measured 2026-09-16, after an update run was reported as hung.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_backup import DEFAULT_EXCLUDES  # noqa: E402, I001

# Docker's default when a service declares no stop_grace_period. A store that
# checkpoints on close needs more than this -- ADR-0041.
DOCKER_DEFAULT_STOP = 10

# How long to wait for a service to report healthy after `up -d`. Jellyfin's
# 10.x -> 12.0 migration rewrote a 1.1 GB database; the health check is red for
# the whole of it, which is also why jellyfin carries no autoheal label.
HEALTH_TIMEOUT_S = 900
HEALTH_POLL_S = 10


class Kind(StrEnum):
  """How a service's image is sourced, which decides how it updates."""

  DRIFT = "drift"      # floating tag (:latest, :nightly) -- pull moves it
  PINNED = "pinned"    # an explicit version -- needs a repo edit to move
  BUILT = "built"      # built here; a pull does nothing, only --build moves it


class Store(StrEnum):
  """What a service keeps, which decides how it must be backed up."""

  NONE = "none"
  SQLITE = "sqlite"
  POSTGRES = "postgres"


# Tags that are moving pointers, not releases. Same list the diun emitter
# uses, and for the same reason: ranked by version they can sort ABOVE real
# releases in some registries.
FLOATING = frozenset(
  {"latest", "nightly", "develop", "development", "edge", "main",
   "master", "unstable", "beta", "dev"}
)

# Services whose store must be captured before the image moves, and how.
# `postgres` cannot be tarred from the host: PGDATA is `drwx------ 999:tom`,
# so `du -sh` reports 4.0K against gigabytes the container sees, and a
# filesystem backup would "succeed" and be empty.
STORES: dict[str, Store] = {
  "jellyfin": Store.SQLITE,
  "sonarr": Store.SQLITE,
  "radarr": Store.SQLITE,
  "lidarr": Store.SQLITE,
  "bazarr": Store.SQLITE,
  "prowlarr": Store.SQLITE,
  "qbittorrent": Store.SQLITE,
  "tinyauth": Store.SQLITE,
  "beszel": Store.SQLITE,
  "streamystats-db": Store.POSTGRES,
  "playlist-generator-db": Store.POSTGRES,
}

# Services that migrate their store ONE WAY on first start of a new version.
# For these a tag is not a rollback: reverting leaves the old binary unable to
# open its own data. tinyauth v5.2.0 migrates to schema 11 and v5.1.3 has no
# down-migration, so the old binary crash-loops -- and because swag declares
# `depends_on: tinyauth: service_healthy`, that takes SWAG down with it,
# turning a protected-routes outage into a total one (ADR-0036).
ONE_WAY = frozenset({"jellyfin", "tinyauth", "streamystats-db",
                     "playlist-generator-db"})

# Postgres 18 moved where the official images keep the cluster: under a
# major-versioned subdirectory (/var/lib/postgresql/18/docker) so that
# `pg_upgrade --link` never crosses a mount boundary. The images REFUSE TO
# START when anything is mounted at the old /var/lib/postgresql/data -- empty
# or not. So a pg17 -> pg18 bump is a compose *volume* change as well as a tag
# change. docker-library/postgres#1259; measured here 2026-09-15, where pg18
# crash-looped for 15 minutes with the tag bumped and the mount untouched.
PG_PARENT_MOUNT_MAJOR = 18
PGDATA_LEGACY_TARGET = "/var/lib/postgresql/data"
PGDATA_PARENT_TARGET = "/var/lib/postgresql"

# Host cron jobs that write a service's store while an upgrade is in flight.
# Holding the job's own flock is better than editing the crontab: the jobs skip
# cleanly instead of failing and alerting, and there is nothing to forget to
# restore afterwards.
CRON_LOCKS: dict[str, str] = {
  "playlist-generator-db": "/tmp/nas-playlist-stage.lock",
}

# Services whose failure takes others with them. Used to order the plan and to
# widen the blast radius reported in the plan output.
DEPENDANTS: dict[str, tuple[str, ...]] = {
  "tinyauth": ("swag",),
  "swag": ("every public route",),
  "streamystats-db": ("streamystats", "streamystats-jobs"),
  "playlist-generator-db": ("playlist-generator",),
}


# ---------------------------------------------------------------------------
# pure logic -- everything below this line is testable without Docker
# ---------------------------------------------------------------------------


def is_floating(tag: str) -> bool:
  """True for a moving pointer like `latest` or `nightly-20260914`."""
  base = tag.split("-", 1)[0].lower()
  return base in FLOATING or tag.lower() in FLOATING


def tag_of(image: str) -> str:
  """The tag in an image reference, or "" when it carries none.

  Splitting on the last `:` is not enough: `registry.local:5000/app` has a
  colon in its *host*, and reading `5000/app` as a tag classifies an
  untagged (therefore floating) image as pinned. Only a colon after the last
  `/` is a tag separator.
  """
  last_slash = image.rfind("/")
  colon = image.rfind(":")
  return image[colon + 1:] if colon > last_slash else ""


def repo_of(image: str) -> str:
  """The repository part of an image reference, tag stripped."""
  tag = tag_of(image)
  return image[: -(len(tag) + 1)] if tag else image


def classify(image: str, has_build: bool) -> Kind:
  """Which update shape a service has.

  `has_build` wins: a service with a `build:` section is built here even if it
  also names an image, because that image is the local tag it is built INTO.
  A pull against it does nothing, which is the trap `pnpm update` falls into.
  """
  if has_build:
    return Kind.BUILT
  tag = tag_of(image)
  if not tag:
    return Kind.DRIFT          # no tag at all == :latest
  return Kind.DRIFT if is_floating(tag) else Kind.PINNED


def store_of(service: str) -> Store:
  """What `service` keeps. Unknown services are assumed stateless."""
  return STORES.get(service, Store.NONE)


def is_one_way(service: str) -> bool:
  """True if reverting the tag would leave the old binary unable to read its data."""
  return service in ONE_WAY


def rank_key(tag: str) -> tuple[int, ...]:
  """Order tags by every numeric run in them, left to right.

  This is deliberately NOT semver. The tags that matter here are not semver
  and a strict parser drops them silently, which is how diun came to report
  `5.2.3_v2.0.14-ls475` as newest two days after `ls476` shipped:

      12.1ubu2604-ls49        -> (12, 1, 2604, 49)
      5.2.3_v2.0.14-ls476     -> (5, 2, 3, 2, 0, 14, 476)
      v2.20.0                 -> (2, 20, 0)
      v0.9.4-omnibus          -> (0, 9, 4)

  Comparing same-shaped tuples gives the right answer for all of them, and
  crucially ranks `ls476 > ls475` and `2.20.0 > 2.9.0` -- both of which a
  lexicographic sort gets wrong.
  """
  return tuple(int(n) for n in re.findall(r"\d+", tag))


def same_shape(a: str, b: str) -> bool:
  """True if two tags have the same layout, so rank_key can compare them.

  `12.1ubu2604-ls49` and `nightly-2026091410ubu2604-ls101` both yield four
  numbers, but they are not comparable releases. Comparing the non-numeric
  skeleton is what separates them.
  """
  skel = lambda t: re.sub(r"\d+", "#", t)  # noqa: E731
  return skel(a) == skel(b)


def newer_tags(current: str, candidates: list[str]) -> list[str]:
  """Candidates that are the same shape as `current` and rank above it.

  Returned newest-first. A candidate of a different shape is ignored rather
  than guessed at -- an upgrade path this cannot name is one a human should.
  """
  cur = rank_key(current)
  same = [t for t in candidates
          if t != current and same_shape(t, current) and rank_key(t) > cur]
  return sorted(same, key=rank_key, reverse=True)


def needs_backup(service: str, kind: Kind) -> bool:
  """Whether this service must be backed up before its image moves.

  A `built` service has no registry update to apply, so nothing to guard
  against. Everything with a store is guarded, one-way or not: the one-way
  list decides whether a *missing* backup is fatal, not whether to take one.
  """
  if kind is Kind.BUILT:
    return False
  return store_of(service) is not Store.NONE


def grace_seconds(raw: str | int | None) -> int | None:
  """Parse a compose duration ('2m0s', '90s', '1m', 120) to seconds.

  None when absent or unparseable -- both of which mean "Docker's default
  applies", which for a service holding CAP_KILL is the ADR-0041 defect.
  """
  if raw is None:
    return None
  s = str(raw).strip()
  if not s:
    return None
  if s.isdigit():
    return int(s)
  m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", s)
  if not m or not any(m.groups()):
    return None
  h, mi, se = m.groups()
  return int(int(h or 0) * 3600 + int(mi or 0) * 60 + float(se or 0))


def stop_is_truncated(caps: list[str], grace: str | int | None) -> bool:
  """True when a service can receive SIGTERM but not finish acting on it.

  The ADR-0041 shape: CAP_KILL present (so s6 can signal across the uid
  boundary) but no headroom over Docker's 10s default, so the shutdown is
  SIGKILLed partway through and the SQLite WAL is left dirty. The two look
  identical from outside -- `docker compose stop` returns 0 either way.
  """
  held = {str(c).upper().removeprefix("CAP_") for c in caps}
  if "KILL" not in held:
    return False
  secs = grace_seconds(grace)
  return secs is None or secs <= DOCKER_DEFAULT_STOP


def dirty_wals(entries: list[tuple[str, int]]) -> list[tuple[str, int]]:
  """The `-wal` files that still hold frames, largest first.

  Content, not existence. A `-wal` holds committed frames not yet folded into
  the `.db`, so a NON-EMPTY one is what makes a `.db`-only copy stale. A
  0-byte one is the opposite: it is the receipt for a checkpoint that already
  happened. And a `-shm` is a shared-memory index INTO the `-wal` -- it holds
  no committed data of its own, so it is evidence of nothing either way.

  SQLite normally unlinks both on a clean close, but a connection that
  persists its WAL leaves them at 0 bytes instead; jellyfin's introskipper
  plugin does exactly that.
  """
  return sorted(((p, n) for p, n in entries if p.endswith("-wal") and n > 0),
                key=lambda e: -e[1])


def wal_is_clean(entries: list[tuple[str, int]]) -> bool:
  """True when a stopped SQLite service left nothing un-checkpointed.

  This is the assertion that separates a real stop from a SIGKILL that
  `docker compose stop` still reported as success. A dirty WAL here means any
  backup copying the `.db` alone is stale -- silently, every time (ADR-0041).
  """
  return not dirty_wals(entries)


def backup_filter_rules(excludes: list[str] | None = None) -> list[str]:
  """`rsync` filter arguments for a cold pre-upgrade copy.

  Two properties of rsync's filter engine decide the shape of this, and
  getting either wrong fails quietly rather than loudly. The FIRST matching
  rule wins, so every re-include has to precede the excludes; and an excluded
  directory is never descended into, so a re-include of something beneath one
  must name each parent directory as well or nothing can ever reach it.
  """
  patterns = DEFAULT_EXCLUDES if excludes is None else excludes
  includes: list[str] = []
  for pat in patterns:
    if not pat.startswith("!"):
      continue
    keep = pat[1:]
    parts = keep.rstrip("*").strip("/").split("/")
    for depth in range(1, len(parts) + 1):
      rule = f"--include={'/'.join(parts[:depth])}/"
      if rule not in includes:
        includes.append(rule)
    includes.append(f"--include={keep}")
  return includes + [f"--exclude={p}" for p in patterns if not p.startswith("!")]


def needs_parent_mount(target_major: int | None, mount_target: str) -> bool:
  """True when moving to this major also requires moving the bind mount up.

  Postgres >= 18's images hard-error on a mount at `/var/lib/postgresql/data`,
  so this is not a tidiness question: without the volume change the new major
  never starts, and the only symptom is a restart loop with a green plan.
  """
  return (target_major is not None
          and target_major >= PG_PARENT_MOUNT_MAJOR
          and mount_target == PGDATA_LEGACY_TARGET)


def rewrite_pgdata_mount(text: str, service: str) -> tuple[str, int]:
  """Point `<anything>/<service>:/var/lib/postgresql/data` at the parent.

  Matches the RAW compose text, where the source is still `${CONFIG_DIRECTORY}
  /<service>` -- the resolved path only exists in the model.
  """
  pattern = re.compile(
    rf"^(\s*-\s*)(\S*{re.escape(service)}):{re.escape(PGDATA_LEGACY_TARGET)}[ \t]*$",
    re.M,
  )
  return pattern.subn(
    lambda m: f"{m.group(1)}{m.group(2)}:{PGDATA_PARENT_TARGET}", text
  )


def bump_image_line(text: str, old_image: str, new_image: str) -> tuple[str, int]:
  """Replace an exact `image: <old>` line, returning the text and hit count.

  Anchored on the `image:` key rather than a bare substring: the same image
  string appears in comments and in the diun manifest, and a loose replace
  would rewrite prose. Returns the count so the caller can refuse on 0 (the
  tag is not where we thought) or on >1 (two services share an image and this
  would move both).
  """
  # `[ \t]*$`, never `\s*$`: \s crosses newlines, so a match on the file's
  # last line swallows its terminator and welds it to the next line.
  pattern = re.compile(rf"^(\s*image:\s*){re.escape(old_image)}[ \t]*$", re.M)
  new, n = pattern.subn(lambda m: f"{m.group(1)}{new_image}", text)
  return new, n


def pg_env(svc: dict) -> tuple[str, str, str]:
  """(user, password, db) from a Postgres service's resolved environment.

  `docker compose config` substitutes `.env`, so the credentials are already
  here -- there is never a reason to ask an operator for them, and an earlier
  version of this script refused a Postgres backup for exactly that made-up
  reason. A refusal that had a readable answer sitting in the model is just a
  missing feature wearing a safety label.
  """
  env = svc.get("environment") or {}
  if isinstance(env, list):
    env = dict(x.split("=", 1) for x in env if "=" in x)
  user = env.get("POSTGRES_USER") or "postgres"
  return user, env.get("POSTGRES_PASSWORD") or "", env.get("POSTGRES_DB") or user


def pgdata_path(svc: dict) -> Path | None:
  """The host path bound at PGDATA, from the model rather than a convention."""
  for v in svc.get("volumes") or []:
    if isinstance(v, dict) and v.get("target") in (PGDATA_LEGACY_TARGET,
                                                   PGDATA_PARENT_TARGET):
      src = v.get("source")
      return Path(src) if src else None
  return None


def pg_major(tag: str) -> int | None:
  """The Postgres major in a tag: `pg16`, `pg17-v0.4.1`, `18-alpine` -> 16/17/18."""
  m = re.search(r"(?:^|[^a-z0-9])pg(\d+)", tag, re.I)
  if m:
    return int(m.group(1))
  m = re.match(r"^(\d+)(?:[.\-].*)?$", tag)
  return int(m.group(1)) if m else None


def is_pg_major_change(current: str, target: str) -> bool:
  """True when the cluster must be dumped and restored, not just restarted.

  Postgres refuses to start against a PGDATA whose `PG_VERSION` does not match
  the binary, so this is the one upgrade in the stack where moving the tag
  alone leaves a service that cannot come up at all.
  """
  a, b = pg_major(current), pg_major(target)
  return a is not None and b is not None and a != b


@dataclass(frozen=True)
class Action:
  """One service's planned update, and everything needed to judge it."""

  service: str
  kind: Kind
  current: str
  target: str | None = None              # None == already current
  store: Store = Store.NONE
  one_way: bool = False
  dependants: tuple[str, ...] = ()

  @property
  def behind(self) -> bool:
    return self.target is not None and self.target != self.current

  @property
  def needs_proof(self) -> bool:
    """A missing backup is fatal here, not a warning."""
    return self.behind and self.one_way

  def describe(self) -> str:
    if not self.behind:
      return f"{self.service}: current ({self.current})"
    bits = [f"{self.service}: {self.current} -> {self.target}"]
    if self.store is not Store.NONE:
      bits.append(f"backup={self.store.value}")
    if self.one_way:
      bits.append("ONE-WAY (a tag is not a rollback)")
    if self.dependants:
      bits.append(f"takes down: {', '.join(self.dependants)}")
    return "  ".join(bits)


def build_plan(
  services: dict[str, dict],
  newest: dict[str, str | None],
  only: set[str] | None = None,
  kinds: set[Kind] | None = None,
) -> list[Action]:
  """Turn the compose model plus a tag lookup into an ordered action list.

  Ordering puts services that nothing depends on first and blast-radius
  services last, so a run that halts on a failure has broken as little as
  possible. `swag` and `tinyauth` therefore move after the *arr apps.
  """
  actions: list[Action] = []
  for name, svc in sorted(services.items()):
    if only and name not in only:
      continue
    image = str(svc.get("image") or "")
    kind = classify(image, bool(svc.get("build")))
    if kinds and kind not in kinds:
      continue
    current = tag_of(image)
    actions.append(
      Action(
        service=name,
        kind=kind,
        current=current or "latest",
        target=newest.get(name),
        store=store_of(name),
        one_way=is_one_way(name),
        dependants=DEPENDANTS.get(name, ()),
      )
    )
  return sorted(actions, key=lambda a: (len(a.dependants), a.service))


def verdict(applied: int, skipped: int, failed: int) -> int:
  """Map a run's outcome onto the scripts/ exit contract.

  A failure is 2 and not 1 on purpose: `cron_job.py` treats 0 and 1 alike, so
  a partial exit cannot alert. That is exactly how the Lidarr bridge sat
  broken for a day -- warning-plus-exit-0 is invisible here (ADR-0003).
  """
  if failed:
    return 2
  if skipped:
    return 1
  _ = applied
  return 0


# ---------------------------------------------------------------------------
# impure -- Docker, the registry, the filesystem
# ---------------------------------------------------------------------------


def run(cmd: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
  """Run a command, never raising for a missing or hung binary.

  A traceback out of a helper is worse than a bad exit code here: this script
  runs unattended, and the caller already treats a non-zero return as "could
  not determine", which is the honest answer in both cases.
  """
  try:
    return subprocess.run(
      cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout, check=False
    )
  except FileNotFoundError:
    return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found")
  except subprocess.TimeoutExpired:
    return subprocess.CompletedProcess(cmd, 124, "", f"{cmd[0]}: timed out after {timeout}s")


def compose_model() -> dict:
  """The fully-merged compose model. Fatal if it will not render."""
  p = run(["docker", "compose", "config", "--format", "json"], timeout=120)
  if p.returncode != 0:
    raise SystemExit(f"compose model unreadable: {p.stderr.strip()[:400]}")
  return json.loads(p.stdout)


def _registry_endpoint(repo: str) -> tuple[str, str]:
  """Split an image repo into (registry host, repository path).

  Docker Hub is implicit and its official images live under `library/`:
  `nginx` is really `registry-1.docker.io/library/nginx`. A first segment with
  no dot and no colon is therefore a namespace, not a host.
  """
  head, _, rest = repo.partition("/")
  if not rest or ("." not in head and ":" not in head and head != "localhost"):
    path = repo if "/" in repo else f"library/{repo}"
    return "registry-1.docker.io", path
  return head, rest


def _bearer(host: str, path: str, timeout: int) -> str:
  """Fetch an anonymous pull token, using the realm the registry itself names.

  Hard-coding a token endpoint breaks on every registry that is not the one
  you tested: lscr.io answers with a realm of ghcr.io, and guessing that would
  silently return nothing -- which reads as "no updates".
  """
  url = f"https://{host}/v2/{path}/tags/list"
  try:
    urllib.request.urlopen(url, timeout=timeout)  # noqa: S310
    return ""
  except urllib.error.HTTPError as e:
    if e.code != 401:
      return ""
    challenge = e.headers.get("WWW-Authenticate", "")
  except OSError:
    return ""
  parts = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
  realm = parts.pop("realm", "")
  if not realm:
    return ""
  try:
    with urllib.request.urlopen(  # noqa: S310
      f"{realm}?{urllib.parse.urlencode(parts)}", timeout=timeout
    ) as r:
      body = json.loads(r.read())
    return body.get("token") or body.get("access_token") or ""
  except (OSError, json.JSONDecodeError):
    return ""


def registry_tags(image: str, timeout: int = 30, max_pages: int = 20) -> list[str]:
  """Every tag of `image`'s repository, straight from the registry API.

  Paginated: GHCR caps a page at 1000 and returns the oldest tags first, so a
  single unpaginated page can contain *none* of the current releases. That is
  not hypothetical -- the first 1000 tags of linuxserver/qbittorrent contain
  zero release-shaped tags.

  An empty list means UNKNOWN, never "no updates available". The caller must
  keep that distinction: reporting "up to date" because a lookup failed is the
  silent-failure shape this whole script exists to avoid (ADR-0024).
  """
  repo = repo_of(image)
  host, path = _registry_endpoint(repo)
  token = _bearer(host, path, timeout)
  url = f"https://{host}/v2/{path}/tags/list?n=1000"
  headers = {"Authorization": f"Bearer {token}"} if token else {}
  tags: list[str] = []
  for _ in range(max_pages):
    try:
      req = urllib.request.Request(url, headers=headers)  # noqa: S310
      with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        page = json.loads(r.read())
        link = r.headers.get("Link", "")
    except (OSError, json.JSONDecodeError):
      return tags
    tags.extend(page.get("tags") or [])
    m = re.search(r"<([^>]+)>\s*;\s*rel=\"?next\"?", link)
    if not m:
      break
    nxt = m.group(1)
    url = nxt if nxt.startswith("http") else f"https://{host}{nxt}"
  return tags


def image_id(ref: str) -> str | None:
  p = run(["docker", "image", "inspect", "-f", "{{.Id}}", ref], timeout=60)
  return p.stdout.strip() if p.returncode == 0 else None


def running_image_id(service: str) -> str | None:
  p = run(["docker", "compose", "ps", "-q", service], timeout=60)
  cid = p.stdout.strip()
  if not cid:
    return None
  q = run(["docker", "inspect", "-f", "{{.Image}}", cid], timeout=60)
  return q.stdout.strip() if q.returncode == 0 else None


def pull(service: str, timeout: int = 1800) -> bool:
  p = run(["docker", "compose", "pull", service], timeout=timeout)
  return p.returncode == 0


def wait_healthy(service: str, timeout: int = HEALTH_TIMEOUT_S) -> tuple[bool, str]:
  """Poll until `service` is healthy or has no healthcheck at all.

  A service without a healthcheck is judged on being `running`, and says so --
  an honest weaker claim beats a green tick that means nothing. ADR-0040: a
  green healthcheck is not reachability either; jellyfin once curled its own
  localhost happily while attached to no network.
  """
  deadline = time.time() + timeout
  last = "unknown"
  restarts = 0
  while time.time() < deadline:
    p = run(["docker", "compose", "ps", "--format", "json", service], timeout=60)
    rows = [json.loads(ln) for ln in p.stdout.splitlines() if ln.strip()]
    if rows:
      row = rows[0]
      state, health = row.get("State", ""), (row.get("Health") or "").lower()
      last = f"{state}/{health or 'no healthcheck'}"
      if health == "healthy":
        return True, last
      if not health and state == "running":
        return True, f"{last} (running; no healthcheck to prove more)"
      if health == "unhealthy":
        return False, last
      # A restart loop will never become healthy; waiting the full timeout
      # just delays the diagnosis. pg18 crash-looped for 15 minutes here
      # before this check existed.
      if state.lower() == "restarting":
        restarts += 1
        if restarts >= 3:
          return False, f"{last} -- restart loop, not a slow start"
    time.sleep(HEALTH_POLL_S)
  return False, f"timeout after {timeout}s (last: {last})"


def sqlite_backup(service: str, config_dir: Path, dest: Path) -> tuple[bool, str]:
  """Stop the service, prove the WAL checkpointed, then copy the tree.

  The stop is not optional and neither is the assertion. A live `cp` of a
  WAL-mode database reads back stale, and a stop that was really a SIGKILL
  leaves the WAL dirty while still reporting success -- so this checks the
  files rather than the exit code (ADR-0041).
  """
  src = config_dir / service
  if not src.exists():
    return False, f"{src} does not exist"
  run(["docker", "compose", "stop", service], timeout=600)
  code = run(["docker", "inspect", "-f", "{{.State.ExitCode}}", service], timeout=60)
  exit_code = code.stdout.strip()
  leftovers = [(str(p), p.stat().st_size) for p in src.rglob("*")
               if p.name.endswith(("-wal", "-shm")) and p.is_file()]
  dest.mkdir(parents=True, exist_ok=True)
  # The service is down from the `stop` above until the copy returns, and
  # `run` captures output, so an unannounced copy is indistinguishable from a
  # hang. That is exactly how this step was reported on 2026-09-16.
  print(f"    copying {service}'s store -> {dest / service} "
        "(regenerable caches/artwork skipped; service is down until this ends)")
  copied, how = copy_tree(config_dir, service, dest)
  if not copied:
    return False, how
  dirty = dirty_wals(leftovers)
  if dirty:
    named = ", ".join(f"{Path(p).name} ({n:,} bytes)" for p, n in dirty[:3])
    return (
      False,
      f"{service} stopped with exit {exit_code} but left {len(dirty)} "
      f"un-checkpointed WAL(s): {named}. The copy is of a database "
      "mid-write. Give it a stop_grace_period above Docker's 10s default "
      "(ADR-0041) and retry.",
    )
  return True, f"{service} stopped cleanly (exit {exit_code}), WAL checkpointed; {how}"


def copy_tree(config_dir: Path, service: str, dest: Path) -> tuple[bool, str]:
  """Copy one service's config tree into `dest/<service>`, minus what regenerates.

  `rsync -R` from the config ROOT with a `/./` pivot, so the transferred paths
  carry the `<service>/` prefix that the shared exclude patterns are written
  against -- they say `jellyfin/data/metadata/**`, not `data/metadata/**`.
  The result is `dest/<service>`, the same shape the `cp -a` this replaced
  produced.

  The database assertion at the end is not ceremony. Skipping subtrees is the
  whole point of this function, and an over-broad pattern would show up as a
  fast, clean, empty backup -- which is worse than the slow one it replaced,
  and would only be discovered on the day someone needed to roll back.
  """
  p = run(["rsync", "-a", "-R", "--stats", *backup_filter_rules(),
           f"{config_dir}/./{service}", f"{dest}/"], timeout=3600)
  if p.returncode != 0:
    return False, f"copy failed: {p.stderr.strip()[:200]}"

  n = size = "?"
  for line in p.stdout.splitlines():
    if line.startswith("Number of regular files transferred:"):
      n = line.split(":", 1)[1].strip()
    elif line.startswith("Total transferred file size:"):
      size = line.split(":", 1)[1].strip().split(" bytes")[0]

  if not any(q.is_file() for q in (dest / service).rglob("*.db")):
    return False, (
      f"copied {n} file(s) to {dest / service} but no database landed there. "
      "Every service with a store here keeps at least one *.db, so this is "
      "not a backup -- check the exclude patterns before retrying."
    )
  return True, f"copied {n} file(s), {size} bytes (regenerable subtrees skipped)"


def postgres_backup(service: str, client_image: str, user: str, db: str,
                    password: str, dest: Path) -> tuple[bool, str, Path | None]:
  """pg_dump into `dest`, then prove it by reading it back.

  `client_image` must be the NEWER of the two servers -- pg_dump refuses a
  server newer than itself, and dumping an old server with a new client is the
  supported direction. A filesystem tar is never a substitute here and is not
  merely worse: PGDATA is `drwx------ 999:tom`, so a host-side tar "succeeds"
  at 4.0K against gigabytes.
  """
  dest.mkdir(parents=True, exist_ok=True)
  out = dest / f"{service}.dump"
  p = run(
    ["docker", "run", "--rm", "--network", "nas-network",
     "-e", f"PGPASSWORD={password}", "-v", f"{dest}:/out", client_image,
     "pg_dump", "-h", service, "-U", user, "-d", db, "-Fc", "-f", f"/out/{out.name}"],
    timeout=7200,
  )
  if p.returncode != 0:
    return False, f"pg_dump failed: {p.stderr.strip()[:300]}", None
  # Prove it by listing it. An exit code says the command ran, not that the
  # archive holds anything -- and an empty archive is the failure that looks
  # most like a success.
  toc = pg_restore_list(client_image, dest, out.name)
  if not toc:
    return False, "dump is unreadable or holds no tables/extensions", None
  size_mb = out.stat().st_size // 1024 // 1024 if out.exists() else 0
  return True, f"{out.name} {size_mb}MB, {len(toc)} objects verified", out


def pg_restore_list(client_image: str, dest: Path, name: str) -> list[str]:
  """The dump's table-of-contents entries that carry data or extensions."""
  q = run(["docker", "run", "--rm", "-v", f"{dest}:/out", client_image,
           "pg_restore", "-l", f"/out/{name}"], timeout=900)
  if q.returncode != 0:
    return []
  return [ln for ln in q.stdout.splitlines()
          if "TABLE DATA" in ln or "EXTENSION" in ln]


def pg_table_count(service: str, user: str, db: str, password: str) -> int | None:
  """User tables in the live database. The restore's proof-by-effect."""
  q = run(["docker", "exec", "-e", f"PGPASSWORD={password}", service,
           "psql", "-U", user, "-d", db, "-tAc",
           "select count(*) from information_schema.tables "
           "where table_schema not in ('pg_catalog','information_schema')"],
          timeout=300)
  if q.returncode != 0:
    return None
  try:
    return int(q.stdout.strip())
  except ValueError:
    return None


def postgres_major_upgrade(service: str, svc: dict, target_image: str,
                           dest: Path) -> tuple[bool, str]:
  """Dump, cut PGDATA aside, initdb on the new major, restore, verify.

  Postgres refuses to start against a PGDATA whose `PG_VERSION` does not match
  the binary, so this is the one upgrade here where moving the tag alone leaves
  a service that cannot come up at all. The sequence is load-bearing:

  * the dump is taken while the OLD server is still running, with the NEW
    client, and is proved before anything is touched;
  * dependants stop FIRST -- `playlist-generator` initialises its schema on
    startup, so a dependant that reaches the new empty cluster before the
    restore lands you in `relation already exists` halfway through;
  * PGDATA is **renamed**, never deleted. A same-filesystem `mv` on a
    tom-owned parent needs no sudo and is what makes this revertible at all;
  * the restore runs before dependants come back.

  On any failure it stops and names the exact revert, because the old cluster
  is still sitting there intact under its timestamped name.
  """
  user, password, db = pg_env(svc)
  pgdata = pgdata_path(svc)
  if pgdata is None:
    return False, "no PGDATA bind mount found in the compose model"
  if not password:
    return False, "POSTGRES_PASSWORD is empty in the resolved model"

  deps = [d for d in DEPENDANTS.get(service, ()) if " " not in d]

  print(f"    [1/7] dump with the new client ({tag_of(target_image)})")
  ok, why, dump = postgres_backup(service, target_image, user, db, password, dest)
  print(f"          {why}")
  if not ok or dump is None:
    return False, f"refusing to touch PGDATA without a proven dump: {why}"
  before = pg_table_count(service, user, db, password)
  print(f"          live table count before: {before}")

  print(f"    [2/7] stop dependants first: {', '.join(deps) or '(none)'}")
  for d in deps:
    run(["docker", "compose", "stop", d], timeout=600)
  run(["docker", "compose", "stop", service], timeout=900)

  aside = pgdata.with_name(f"{pgdata.name}.pre-pg{pg_major(tag_of(target_image))}-"
                           f"{time.strftime('%Y%m%d-%H%M%S')}")
  print(f"    [3/7] rename PGDATA aside -> {aside.name}")
  mv = run(["mv", str(pgdata), str(aside)], timeout=600)
  if mv.returncode != 0:
    for d in [service, *deps]:
      run(["docker", "compose", "up", "-d", d], timeout=600)
    return False, f"could not rename PGDATA: {mv.stderr.strip()[:200]} (nothing changed)"

  revert = (f"docker compose stop {service}; rm -rf {pgdata}; "
            f"mv {aside} {pgdata}; git checkout -- compose/ && docker compose up -d {service}")

  want_major = pg_major(tag_of(target_image))
  mount_target = next((v.get("target") for v in (svc.get("volumes") or [])
                       if isinstance(v, dict)
                       and v.get("target") in (PGDATA_LEGACY_TARGET,
                                               PGDATA_PARENT_TARGET)), "")
  if needs_parent_mount(want_major, str(mount_target)):
    moved, mwhy = move_pgdata_mount(service)
    print(f"    [3b/7] pg{want_major} layout change -- "
          f"{'ok' if moved else 'FAILED'}: {mwhy}")
    if not moved:
      return False, (f"pg{want_major} needs the mount at {PGDATA_PARENT_TARGET} "
                     f"and it could not be moved: {mwhy}. REVERT: {revert}")

  print("    [4/7] start the new major (initdb makes a fresh cluster)")
  up = run(["docker", "compose", "up", "-d", "--force-recreate", service], timeout=1800)
  if up.returncode != 0:
    return False, f"new major would not start: {up.stderr.strip()[:200]}. REVERT: {revert}"
  healthy, how = wait_healthy(service)
  print(f"          health: {how}")
  if not healthy:
    return False, f"new major did not become healthy: {how}. REVERT: {revert}"

  # Ask the SERVER what it is, before pouring data into it. A healthy
  # container on the old major would happily accept the restore and leave the
  # upgrade silently un-done -- which is exactly what happened on the first
  # run of this, when the compose tag had not been edited.
  want = pg_major(tag_of(target_image))
  live_v = run(["docker", "exec", "-e", f"PGPASSWORD={password}", service,
                "psql", "-U", user, "-d", "postgres", "-tAc", "show server_version"],
               timeout=300).stdout.strip()
  live_major = pg_major(live_v.split(".")[0]) if live_v else None
  print(f"          server_version: {live_v or '(unreadable)'}")
  if live_major != want:
    return False, (f"started on Postgres {live_major}, expected {want} -- the "
                   f"recreate did not take the new tag. REVERT: {revert}")

  print("    [5/7] restore into the empty cluster (before dependants start)")
  rs = run(["docker", "run", "--rm", "--network", "nas-network",
            "-e", f"PGPASSWORD={password}", "-v", f"{dest}:/out", target_image,
            "pg_restore", "-h", service, "-U", user, "-d", db,
            "--no-owner", "--no-privileges", f"/out/{dump.name}"],
           timeout=7200)
  # pg_restore exits non-zero on benign notices (an extension already present
  # from the template, for one), so the exit code is not the verdict here --
  # the row it produced is.
  after = pg_table_count(service, user, db, password)
  print(f"    [6/7] verify: {before} tables before -> {after} after "
        f"(pg_restore exit {rs.returncode})")
  if after is None or after == 0:
    return False, (f"restore produced {after} tables. REVERT: {revert}")
  if before is not None and after < before:
    return False, (f"restore is INCOMPLETE: {after} tables vs {before} before. "
                   f"REVERT: {revert}")

  print(f"    [7/7] start dependants: {', '.join(deps) or '(none)'}")
  for d in deps:
    run(["docker", "compose", "up", "-d", d], timeout=1800)
    dh, dhow = wait_healthy(d)
    print(f"          {d}: {dhow}")
    if not dh:
      return False, f"{d} did not come back after the restore. REVERT: {revert}"

  return True, (f"pg{pg_major(tag_of(target_image))} live, {after} tables restored; "
                f"old cluster kept at {aside.name} (delete it once you are happy)")


@contextlib.contextmanager
def cron_lock(path: str | None, timeout: int = 120):
  """Hold a host cron job's flock for the window, or yield False if busy.

  Non-blocking with a bounded wait: if a scheduled job is mid-write we would
  rather skip this service than cut its database out from under it.
  """
  if not path:
    yield None
    return
  fh = open(path, "a+")  # noqa: SIM115 -- released in the finally below
  deadline = time.time() + timeout
  try:
    while True:
      try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
      except OSError:
        if time.time() >= deadline:
          yield False
          return
        time.sleep(5)
    yield True
  finally:
    try:
      fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
      fh.close()


def compose_files() -> list[Path]:
  """Every tracked compose file that can declare an image."""
  return sorted([*REPO.glob("compose/*.yaml"), *REPO.glob("webapps/*/compose.yaml"),
                 *REPO.glob("compose.yaml")])


def bump_tag(old_image: str, new_image: str) -> tuple[bool, str]:
  """Edit the pinned tag in whichever compose file declares it.

  This is the step whose absence made every pinned bump a silent no-op: the
  script pulled the target image, ran `up -d`, and compose recreated the
  container from the tag still written in the file. It reported "applied" and
  changed nothing -- measured 2026-09-15 against streamystats-db, which came
  back on pg17 after a "successful" pg18 upgrade.

  The diun manifest is regenerated in the same breath, because `make check`
  asserts it matches the compose model (ADR-0024).
  """
  hits: list[Path] = []
  for f in compose_files():
    text = f.read_text()
    new, n = bump_image_line(text, old_image, new_image)
    if n:
      hits.append(f)
      if n > 1:
        return False, f"{old_image} appears {n} times in {f.name}; refusing to guess"
      f.write_text(new)
  if not hits:
    return False, f"no compose file declares `image: {old_image}`"
  if len(hits) > 1:
    return False, f"{old_image} declared in {len(hits)} files: {[h.name for h in hits]}"
  m = run(["make", "diun-manifest"], timeout=600)
  if m.returncode != 0:
    return False, f"tag bumped in {hits[0].name} but `make diun-manifest` failed"
  return True, f"{hits[0].name} + diun/manifest.yml"


def move_pgdata_mount(service: str) -> tuple[bool, str]:
  """Rewrite the service's PGDATA bind mount to the pg18+ parent layout."""
  hits = []
  for f in compose_files():
    text = f.read_text()
    new, n = rewrite_pgdata_mount(text, service)
    if n == 1:
      f.write_text(new)
      hits.append(f)
    elif n > 1:
      return False, f"{service} has {n} legacy PGDATA mounts in {f.name}"
  if not hits:
    return False, f"no `<src>/{service}:{PGDATA_LEGACY_TARGET}` line found"
  return True, f"{hits[0].name}: mount moved to {PGDATA_PARENT_TARGET}"


def running_tag(service: str) -> str | None:
  """The tag the container is ACTUALLY running, from the container itself."""
  cid = run(["docker", "compose", "ps", "-q", service], timeout=60).stdout.strip()
  if not cid:
    return None
  img = run(["docker", "inspect", "-f", "{{.Config.Image}}", cid], timeout=60)
  return tag_of(img.stdout.strip()) if img.returncode == 0 else None


def gate(target: str, timeout: int = 900) -> tuple[bool, str]:
  """Run a repo gate (`make check`, `make lint`, `make verify-runtime`)."""
  p = run(["make", target], timeout=timeout)
  tail = (p.stdout or p.stderr).strip().splitlines()
  return p.returncode == 0, tail[-1] if tail else f"{target} exit {p.returncode}"


def notify(lane: str, title: str, message: str, enabled: bool = True) -> None:
  """Publish through scripts/notify.py. Never holds a topic literal (ADR-0033)."""
  if not enabled:
    return
  run([sys.executable, "-m", "scripts.notify", "--lane", lane,
       "--title", title, "--message", message], timeout=120)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


@dataclass
class Result:
  applied: list[str] = field(default_factory=list)
  skipped: list[str] = field(default_factory=list)
  failed: list[tuple[str, str]] = field(default_factory=list)


def resolve_targets(services: dict[str, dict], actions_kind: dict[str, Kind],
                    verbose: bool = False) -> dict[str, str | None]:
  """Newest available tag per service, or None when it cannot be determined.

  For a pinned service this asks the registry. For a floating one the tag by
  definition does not move, so the answer is the tag itself -- whether the
  *image behind it* moved is decided later by comparing image IDs, which the
  upgrade skill is explicit is the only authority (digests disagree).
  """
  newest: dict[str, str | None] = {}
  for name, svc in services.items():
    kind = actions_kind.get(name)
    image = str(svc.get("image") or "")
    tag = tag_of(image)
    if kind is Kind.BUILT:
      newest[name] = None
      continue
    if kind is Kind.DRIFT:
      newest[name] = tag or "latest"
      continue
    tags = registry_tags(image)
    if not tags:
      if verbose:
        print(f"    {name}: registry lookup returned nothing -- treating as UNKNOWN")
      newest[name] = None
      continue
    ups = newer_tags(tag, tags)
    newest[name] = ups[0] if ups else tag
  return newest


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("--dry-run", action="store_true",
                  help="print the plan and touch nothing")
  ap.add_argument("--check", action="store_true",
                  help="report what is behind and exit 1 if anything is; apply nothing")
  ap.add_argument("--service", action="append", default=[],
                  help="limit to this service (repeatable)")
  ap.add_argument("--kind", choices=[k.value for k in Kind], action="append",
                  default=[], help="limit to this update shape (repeatable)")
  ap.add_argument("--backup-dir", default="/mnt/drive/backups/stack-update",
                  help="where backups go (must NOT be the root LV)")
  ap.add_argument("--no-notify", action="store_true", help="do not publish to ntfy")
  ap.add_argument("--skip-gates", action="store_true",
                  help="skip make check/lint/verify-runtime (for testing only)")
  args = ap.parse_args()

  notify_on = not args.no_notify
  try:
    model = compose_model()
  except SystemExit as e:
    print(f"FATAL: {e}", file=sys.stderr)
    return 2
  services: dict[str, dict] = model.get("services") or {}
  if not services:
    print("FATAL: compose model has no services", file=sys.stderr)
    return 2

  only = set(args.service) or None
  kinds = {Kind(k) for k in args.kind} or None
  kind_of = {n: classify(str(s.get("image") or ""), bool(s.get("build")))
             for n, s in services.items()}

  print("==> resolving newest available tags")
  newest = resolve_targets(services, kind_of, verbose=True)
  plan = build_plan(services, newest, only=only, kinds=kinds)

  behind = [a for a in plan if a.behind]
  print(f"\n==> plan: {len(behind)} of {len(plan)} service(s) behind\n")
  for a in plan:
    print(("  * " if a.behind else "    ") + a.describe())
  unknown = [a.service for a in plan
             if a.kind is Kind.PINNED and newest.get(a.service) is None]
  if unknown:
    print(f"\n  UNKNOWN (registry lookup failed, NOT 'up to date'): {', '.join(unknown)}")

  if args.check:
    print()
    return 1 if (behind or unknown) else 0
  if args.dry_run:
    print("\n(dry run -- nothing applied)")
    return 0
  if not behind:
    print("\nnothing to do")
    return 1 if unknown else 0

  res = Result()
  cfg = Path(os.environ.get("CONFIG_DIRECTORY", REPO / ".docker-config"))
  backup_root = Path(args.backup_dir) / time.strftime("%Y%m%d-%H%M%S")

  for a in behind:
    print(f"\n==> {a.service}: {a.current} -> {a.target}")
    if a.kind is Kind.BUILT:
      print("    built here; `pull` does nothing. Use `up -d --build`. SKIPPED.")
      res.skipped.append(a.service)
      continue

    svc = services[a.service]

    # A Postgres MAJOR is its own procedure, not a backup followed by a
    # restart: the new binary refuses a PGDATA whose PG_VERSION differs, so
    # `up -d` alone leaves a service that cannot start at all. Dump, cut the
    # old cluster aside, initdb, restore, verify.
    if a.store is Store.POSTGRES and is_pg_major_change(a.current, a.target or ""):
      target_image = f"{repo_of(str(svc.get('image') or ''))}:{a.target}"
      print(f"    postgres MAJOR pg{pg_major(a.current)} -> "
            f"pg{pg_major(a.target or '')}: dump + restore, not a restart")
      bumped, bwhy = bump_tag(str(svc.get("image") or ""), target_image)
      print(f"    tag bump: {'ok' if bumped else 'FAILED'} -- {bwhy}")
      if not bumped:
        res.failed.append((a.service, f"could not bump the pinned tag: {bwhy}"))
        break
      if not pull(a.service):
        res.failed.append((a.service, "pull failed"))
        break
      with cron_lock(CRON_LOCKS.get(a.service)) as held:
        if held is False:
          res.skipped.append(a.service)
          print("    another job holds the cron lock; skipping rather than racing it")
          continue
        ok, why = postgres_major_upgrade(a.service, svc, target_image, backup_root)
      print(f"    {'ok' if ok else 'FAILED'} -- {why}")
      if not ok:
        res.failed.append((a.service, why))
        print("    HALTING.")
        break
      res.applied.append(a.service)
      continue

    if needs_backup(a.service, a.kind):
      if a.store is Store.SQLITE:
        ok, why = sqlite_backup(a.service, cfg, backup_root)
      else:
        user, password, db = pg_env(svc)
        target_image = f"{repo_of(str(svc.get('image') or ''))}:{a.target}"
        ok, why, _ = postgres_backup(a.service, target_image, user, db,
                                     password, backup_root)
      print(f"    backup: {'ok' if ok else 'FAILED'} -- {why}")
      if not ok:
        if a.needs_proof:
          res.failed.append((a.service, f"no proven backup, and this is one-way: {why}"))
          print("    HALTING: a tag is not a rollback for this service.")
          break
        res.skipped.append(a.service)
        continue

    # A pinned tag lives in the compose file, and `up -d` reads it from there.
    # Without this edit the pull is real and the recreate is a no-op.
    if a.kind is Kind.PINNED:
      bumped, bwhy = bump_tag(str(svc.get("image") or ""),
                              f"{repo_of(str(svc.get('image') or ''))}:{a.target}")
      print(f"    tag bump: {'ok' if bumped else 'FAILED'} -- {bwhy}")
      if not bumped:
        res.failed.append((a.service, f"could not bump the pinned tag: {bwhy}"))
        break

    if not pull(a.service):
      res.failed.append((a.service, "pull failed"))
      break
    up = run(["docker", "compose", "up", "-d", a.service], timeout=1800)
    if up.returncode != 0:
      res.failed.append((a.service, f"up -d failed: {up.stderr.strip()[:200]}"))
      break
    healthy, how = wait_healthy(a.service)
    print(f"    health: {how}")
    if not healthy:
      res.failed.append((a.service, f"did not become healthy: {how}"))
      print("    HALTING: not touching the next service with this one broken.")
      break
    # Verify by effect: ask the CONTAINER what it is running, not the plan.
    # Health was green throughout the no-op bump that prompted this check.
    live = running_tag(a.service)
    if a.kind is Kind.PINNED and live != a.target:
      res.failed.append(
        (a.service, f"reports healthy but is running {live!r}, not {a.target!r} "
                    "-- the recreate did not take the new tag")
      )
      print("    HALTING: applied is not the same as running.")
      break
    print(f"    running: {live}")
    res.applied.append(a.service)

  if res.applied and not args.skip_gates:
    print("\n==> gates")
    for target in ("check", "lint", "verify-runtime"):
      ok, line = gate(target)
      print(f"    make {target}: {'ok' if ok else 'FAILED'} -- {line}")
      if not ok:
        res.failed.append((f"make {target}", line))

  code = verdict(len(res.applied), len(res.skipped), len(res.failed))
  print(f"\n==> applied={len(res.applied)} skipped={len(res.skipped)} "
        f"failed={len(res.failed)} -> exit {code}")
  if res.applied:
    print(f"    applied: {', '.join(res.applied)}")
  for svc, why in res.failed:
    print(f"    FAILED {svc}: {why}")

  if res.failed:
    notify("critical", "Stack update failed",
           "\n".join(f"{s}: {w}" for s, w in res.failed), notify_on)
  elif res.applied:
    notify("infra", "Stack update applied",
           f"{len(res.applied)} service(s): {', '.join(res.applied)}", notify_on)
  return code


if __name__ == "__main__":
  sys.exit(main())
