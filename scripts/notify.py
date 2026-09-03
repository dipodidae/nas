#!/usr/bin/env python3
"""The one place anything on this host publishes a notification.

Why this exists
---------------
There used to be one ntfy topic, `nas-alerts`, and everything went to it: a
container that no longer existed, a public tracker flapping for the fourth time
today, an episode finishing its import, an image with a newer tag. Mixed
severities on one topic have exactly one stable outcome — the topic gets muted,
and then the failures it was built to surface are the ones you stop seeing.
That is not hypothetical here: two dead indexers produced 103 messages in 48 h
(ADR-0032), and the fix at the time was to damp the *detector*. This is the
other half: give the messages somewhere to go.

Six lanes, all prefixed `nas-`. The prefix is load-bearing — it makes one
wildcard ACL (`nas-*`) and one glance at the phone's subscription list
sufficient:

    nas-critical   5  no container at all, OOM kill, disk error, backup failed
    nas-attention  4  needs a human today, not now
    nas-media      3  new stuff you can actually watch
    nas-requests   4  jellyseerr: approvals, failures, issues
    nas-infra      2  routine ops, recoveries, the daily digest
    nas-updates    1  diun image-update notifications only

**Severity is carried by the priority; audience is carried by the topic.**
Never encode severity in a topic name: a phone can mute a topic but cannot
un-mute half of one, so `nas-errors` and `nas-warnings` would just become two
muted topics instead of one.

Design rules this file obeys
----------------------------
* **No call site holds a topic literal.** Callers name a *lane*; the topic
  comes from `NTFY_TOPIC_<LANE>` with a `nas-<lane>` default.
  `scripts/check-invariants.sh` asserts no bare literal survives anywhere.
* **Publishing over loopback.** `NTFY_URL` defaults to `http://127.0.0.1:8410`,
  so message contents never leave the box (ADR-0012). Containers cannot use
  that address and use `http://ntfy:8410` instead — ntfy runs as
  `${PUID}:${PGID}` (ADR-0014) and a non-root process cannot bind `:80`.
* **A failure to notify never crashes the caller.** An alerter that takes the
  thing it is watching down with it is worse than no alerter. Every path
  returns a `Result`; nothing raises.
* **`nas-critical` is never delayed and never cooldown-suppressed.** Asserted
  in `check-invariants.sh`, because it is the one lane where a missed message
  is the failure mode the whole system exists to prevent.

Noise controls
--------------
* `dedup_key` + `cooldown` — the same key inside its cooldown is suppressed and
  *counted*, so the daily digest can report what it swallowed. Defaults: 6 h on
  `nas-attention`, 1 h on `nas-infra`, none on `nas-critical`.
* `transition()` — publishes on a state *change*, not on every poll. A `*/5`
  job cannot send the same message 288 times a day, and the clear sends exactly
  one `nas-infra` message at priority 2 tagged `white_check_mark`.
* Quiet hours 23:00–08:00 Europe/Amsterdam — `nas-media`, `nas-infra` and
  `nas-updates` get `X-Delay` instead of immediate delivery. `nas-critical` and
  `nas-requests` are never delayed.

Exit codes (CLI)
----------------
  0  published (or deliberately suppressed by a cooldown)
  1  not published: no token configured, or the POST failed. Non-fatal by
     design — the caller's own work is not in question.
  2  bad usage (unknown lane, missing text)

Environment
-----------
  NTFY_URL            base URL to publish to (default http://127.0.0.1:8410)
  NTFY_TOKEN_SCRIPTS  access token for the write-only `nas-scripts` account
  NTFY_TOPIC_<LANE>   optional per-lane topic override (default nas-<lane>)
  NTFY_QUIET_HOURS    optional "23-8" style override; empty disables quiet hours

Usage
-----
  python -m scripts.notify --lane infra --title "Digest" --message "..." --markdown
  python -m scripts.notify --lane critical --title "Disk" --message "ext4 errors"
  python -m scripts.notify --lane media --title "📺 Show S01E01" --message "..." \
      --tags tv --click https://jellyfin.example.com
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

if "NTFY_TOKEN_SCRIPTS" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE = REPO_ROOT / "logs" / ".notify_state.json"
DEFAULT_URL = "http://127.0.0.1:8410"
TOPIC_PREFIX = "nas-"
LOCAL_TZ = "Europe/Amsterdam"
# 23:00–08:00 local. The end hour is also where a delayed message lands, so the
# two cannot drift apart.
QUIET_START_HOUR = 23
QUIET_END_HOUR = 8
POST_TIMEOUT_S = 10.0
# One retry, not three: the caller is usually a */5 cron job that will be back
# in five minutes anyway, and a long retry chain inside a notifier is how a
# notifier starts delaying the work it was bolted onto.
POST_ATTEMPTS = 2
RETRY_SLEEP_S = 1.5


class Lane(StrEnum):
  """The audience a message is for. Not its severity — that is the priority."""

  CRITICAL = "critical"
  ATTENTION = "attention"
  MEDIA = "media"
  REQUESTS = "requests"
  INFRA = "infra"
  UPDATES = "updates"


# The tag vocabulary, in one place so it is applied consistently. ntfy renders
# these as emoji; an unknown name renders as literal text, which is the tell
# that one was invented at a call site.
TAG_CRITICAL = "rotating_light"
TAG_ATTENTION = "warning"
TAG_TV = "tv"
TAG_MOVIE = "film_projector"
TAG_MUSIC = "musical_note"
TAG_REQUEST = "inbox_tray"
TAG_INFRA = "gear"
TAG_UPDATE = "arrow_up"
TAG_RESOLVED = "white_check_mark"

# Media tags by kind, so `arr_notify.sh` and process_soulseek_imports.py cannot
# disagree about which emoji an album gets.
MEDIA_TAGS = {"tv": TAG_TV, "movie": TAG_MOVIE, "music": TAG_MUSIC}


@dataclass(frozen=True)
class LaneSpec:
  """Everything that is true of a lane regardless of the message in it.

  `delayable` and `default_cooldown_min` are the two knobs that make a lane
  quiet. `nas-critical` has both switched off deliberately: the whole point of
  the lane is that a message in it is never held back or swallowed.
  """

  priority: int
  default_tag: str
  delayable: bool
  default_cooldown_min: float | None


LANES: dict[Lane, LaneSpec] = {
  Lane.CRITICAL: LaneSpec(5, TAG_CRITICAL, delayable=False, default_cooldown_min=None),
  Lane.ATTENTION: LaneSpec(4, TAG_ATTENTION, delayable=False, default_cooldown_min=360.0),
  # No default tag: the caller says tv / film_projector / musical_note, because
  # "new media" with the wrong icon is worse than none.
  Lane.MEDIA: LaneSpec(3, "", delayable=True, default_cooldown_min=None),
  Lane.REQUESTS: LaneSpec(4, TAG_REQUEST, delayable=False, default_cooldown_min=None),
  Lane.INFRA: LaneSpec(2, TAG_INFRA, delayable=True, default_cooldown_min=60.0),
  Lane.UPDATES: LaneSpec(1, TAG_UPDATE, delayable=True, default_cooldown_min=None),
}


@dataclass(frozen=True)
class Message:
  """A fully-resolved publish, before any I/O happens.

  Built by a pure function so the header mapping can be tested without a
  server, which is the half that silently rots — a wrong header name is a 200
  with no effect.
  """

  url: str
  body: bytes
  headers: dict[str, str]
  lane: Lane
  dedup_key: str | None = None


@dataclass
class Result:
  """What happened. Never an exception — see the module docstring."""

  sent: bool = False
  suppressed: bool = False
  reason: str = ""

  def __bool__(self) -> bool:
    # A suppressed message is a success from the caller's point of view: the
    # cooldown did its job. Only a real delivery failure is falsey.
    return self.sent or self.suppressed


@dataclass
class State:
  """The dedupe/cooldown/transition bookkeeping, loaded from one JSON file."""

  cooldowns: dict[str, dict] = field(default_factory=dict)
  conditions: dict[str, dict] = field(default_factory=dict)
  suppressed_total: int = 0

  @classmethod
  def from_dict(cls, data: dict) -> State:
    return cls(
      cooldowns=dict(data.get("cooldowns") or {}),
      conditions=dict(data.get("conditions") or {}),
      suppressed_total=int(data.get("suppressed_total") or 0),
    )

  def to_dict(self) -> dict:
    return {
      "cooldowns": self.cooldowns,
      "conditions": self.conditions,
      "suppressed_total": self.suppressed_total,
    }


# --------------------------------------------------------------------------
# Pure logic
# --------------------------------------------------------------------------


def lane_of(name: str) -> Lane:
  """Resolve a lane name. Raises ValueError so the CLI can exit 2 on a typo."""
  return Lane(name.strip().lower().removeprefix(TOPIC_PREFIX))


def topic_for(lane: Lane, env: dict[str, str] | None = None) -> str:
  """`NTFY_TOPIC_<LANE>` if set, else `nas-<lane>`.

  Deliberately the only function that can produce a topic name, so
  `check-invariants.sh` can assert that no other file contains one.
  """
  source = os.environ if env is None else env
  override = (source.get(f"NTFY_TOPIC_{lane.value.upper()}") or "").strip()
  return override or f"{TOPIC_PREFIX}{lane.value}"


def priority_for(lane: Lane, override: int | None = None) -> int:
  """The lane's default priority, unless the caller pinned one."""
  if override is not None:
    return max(1, min(5, int(override)))
  return LANES[lane].priority


def tags_for(lane: Lane, tags: tuple[str, ...] | list[str] = ()) -> str:
  """Comma-joined tags, falling back to the lane's own tag when given none."""
  chosen = [t for t in tags if t]
  if not chosen and LANES[lane].default_tag:
    chosen = [LANES[lane].default_tag]
  return ",".join(chosen)


def is_quiet_hour(moment: datetime, start: int = QUIET_START_HOUR, end: int = QUIET_END_HOUR) -> bool:
  """True inside the 23:00–08:00 window. Wraps midnight, hence the `or`."""
  hour = moment.hour
  if start == end:
    return False
  if start < end:
    return start <= hour < end
  return hour >= start or hour < end


def delay_for(lane: Lane, moment: datetime | None = None) -> str | None:
  """`X-Delay` value for this lane right now, or None for immediate delivery.

  Only the three chatter lanes are delayable. `nas-critical` and `nas-requests`
  are never held: a request waiting for approval at 01:00 is still waiting at
  08:00, and a critical alert that arrives nine hours late is not an alert.
  """
  if not LANES[lane].delayable:
    return None
  raw = os.getenv("NTFY_QUIET_HOURS")
  start, end = QUIET_START_HOUR, QUIET_END_HOUR
  if raw is not None:
    if not raw.strip():
      return None  # quiet hours explicitly disabled
    try:
      start_s, end_s = raw.split("-", 1)
      start, end = int(start_s), int(end_s)
    except ValueError:
      pass  # malformed override: fall back to the documented window
  now = moment or datetime.now(ZoneInfo(LOCAL_TZ))
  if not is_quiet_hour(now, start, end):
    return None
  return f"{end % 12 or 12}{'am' if end < 12 else 'pm'}"


def cooldown_seconds(lane: Lane, override: float | None) -> float:
  """Cooldown in seconds. `nas-critical` is pinned to zero — never suppressed."""
  if lane is Lane.CRITICAL:
    return 0.0
  if override is not None:
    return max(0.0, float(override) * 60.0)
  default = LANES[lane].default_cooldown_min
  return 0.0 if default is None else default * 60.0


def should_send(state: State, key: str | None, cooldown_s: float, now: float) -> bool:
  """Pure cooldown test. No key or no cooldown means always send."""
  if not key or cooldown_s <= 0:
    return True
  last = float((state.cooldowns.get(key) or {}).get("last_sent") or 0.0)
  return (now - last) >= cooldown_s


def build_message(
  lane: Lane,
  title: str,
  message: str,
  *,
  priority: int | None = None,
  tags: tuple[str, ...] | list[str] = (),
  click: str | None = None,
  markdown: bool = False,
  delay: str | None = None,
  dedup_key: str | None = None,
  base_url: str | None = None,
  token: str | None = None,
) -> Message:
  """Resolve a call into the exact HTTP request that would be made.

  Split out from `publish` so the header mapping is testable: a wrong header
  name does not fail, it returns 200 and does nothing, which is the failure
  mode this repo has already been bitten by three times (AGENTS.md, "when a
  check passes, ask whether it proves the property you care about").
  """
  url = (base_url or os.getenv("NTFY_URL") or DEFAULT_URL).rstrip("/")
  headers = {
    "X-Title": title,
    "X-Priority": str(priority_for(lane, priority)),
    "Content-Type": "text/plain; charset=utf-8",
  }
  tag_value = tags_for(lane, tags)
  if tag_value:
    headers["X-Tags"] = tag_value
  if click:
    headers["X-Click"] = click
  if markdown:
    headers["X-Markdown"] = "true"
  effective_delay = delay if delay is not None else delay_for(lane)
  if effective_delay and lane is not Lane.CRITICAL:
    headers["X-Delay"] = effective_delay
  auth = token if token is not None else os.getenv("NTFY_TOKEN_SCRIPTS", "")
  if auth:
    headers["Authorization"] = f"Bearer {auth}"
  return Message(
    url=f"{url}/{topic_for(lane)}",
    body=message.encode("utf-8"),
    headers=headers,
    lane=lane,
    dedup_key=dedup_key,
  )


# --------------------------------------------------------------------------
# State I/O
# --------------------------------------------------------------------------


def load_state(path: Path | None = None) -> State:
  target = DEFAULT_STATE if path is None else path
  try:
    data = json.loads(target.read_text())
  except (OSError, json.JSONDecodeError):
    return State()
  return State.from_dict(data if isinstance(data, dict) else {})


def save_state(state: State, path: Path | None = None) -> None:
  """Write the state file atomically. A torn write would lose every cooldown."""
  target = DEFAULT_STATE if path is None else path
  try:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=1, sort_keys=True))
    tmp.replace(target)
  except OSError as exc:
    print(f"WARNING: could not write notify state {target}: {exc}", file=sys.stderr)


def prune_state(state: State, now: float, max_age_s: float = 30 * 86400.0) -> int:
  """Drop cooldown/condition entries untouched for `max_age_s`. Returns count.

  Called by `log_pruner.py`: without it the file grows one entry per distinct
  dedup key forever, and the keys include episode and container names.
  """
  dropped = 0
  for bucket, stamp in (("cooldowns", "last_sent"), ("conditions", "changed_at")):
    entries: dict[str, dict] = getattr(state, bucket)
    for key in [k for k, v in entries.items() if now - float(v.get(stamp) or 0) > max_age_s]:
      del entries[key]
      dropped += 1
  return dropped


def suppressed_since(state: State, keys: list[str] | None = None) -> int:
  """Total messages swallowed by a cooldown, for the daily digest."""
  if keys is None:
    return state.suppressed_total
  return sum(int((state.cooldowns.get(k) or {}).get("suppressed") or 0) for k in keys)


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------


def _wire_headers(headers: dict[str, str]) -> dict[str, str]:
  """Re-encode header values so urllib puts UTF-8 bytes on the wire.

  `http.client` encodes header values as **latin-1**, so an `X-Title` with an
  em dash or an emoji raises `UnicodeEncodeError` — which `publish()` catches
  as a `ValueError` and reports as a failed publish. The message is simply
  never sent.

  That is exactly what happened to every title in this file's own vocabulary:
  `📺 The Expanse S02E07`, `🎵 Artist — Album`, `🗒 NAS digest`. It was invisible
  because the failure is a logged warning on a best-effort notifier, and
  because `scripts/arr_notify.sh` uses `curl`, which sends raw bytes and was
  therefore fine — so the shell path worked and the Python path did not.

  Encoding to UTF-8 and decoding as latin-1 hands `http.client` a string it can
  encode 1:1, which puts the original UTF-8 bytes on the wire — byte-identical
  to what curl sends, and what ntfy expects. Done at the wire boundary only, so
  `Message.headers` stays readable for tests and logs.
  """
  return {k: v.encode("utf-8").decode("latin-1") for k, v in headers.items()}


def publish(msg: Message) -> tuple[bool, str]:
  """POST one message. Returns (ok, reason). Never raises."""
  if "Authorization" not in msg.headers:
    # Print-only mode: no token configured (CI, a fresh clone, a test). Still
    # useful — the message lands in the job log and in cron mail.
    print(f"NOTIFY (no token; not sent) [{msg.lane.value}] {msg.headers.get('X-Title', '')}: "
          f"{msg.body.decode('utf-8', 'replace')}")
    return False, "no NTFY_TOKEN_SCRIPTS configured"
  last = "unknown error"
  for attempt in range(1, POST_ATTEMPTS + 1):
    req = urllib.request.Request(
      msg.url, data=msg.body, method="POST", headers=_wire_headers(msg.headers),
    )
    try:
      with urllib.request.urlopen(req, timeout=POST_TIMEOUT_S) as resp:  # noqa: S310
        if 200 <= resp.status < 300:
          return True, f"HTTP {resp.status}"
        last = f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
      last = f"HTTP {exc.code}"
      # 4xx is a configuration error (403 = the ACL says no, 404 = bad topic).
      # Retrying it just doubles the log noise.
      if 400 <= exc.code < 500:
        break
    except (OSError, ValueError) as exc:
      last = str(exc)
    if attempt < POST_ATTEMPTS:
      time.sleep(RETRY_SLEEP_S)
  print(f"WARNING: notify to {msg.lane.value} failed: {last}", file=sys.stderr)
  return False, last


def notify(
  lane: Lane | str,
  title: str,
  message: str,
  *,
  priority: int | None = None,
  tags: tuple[str, ...] | list[str] = (),
  click: str | None = None,
  markdown: bool = False,
  delay: str | None = None,
  dedup_key: str | None = None,
  cooldown: float | None = None,
  state_path: Path | None = None,
) -> Result:
  """Publish one message on `lane`. Never raises; returns what happened.

  `cooldown` is in **minutes** and only applies when `dedup_key` is given.
  A suppressed message increments a counter the daily digest reports, so
  "quiet" and "broken" stay distinguishable.
  """
  try:
    resolved_lane = lane if isinstance(lane, Lane) else lane_of(str(lane))
  except ValueError:
    print(f"WARNING: unknown notify lane {lane!r}", file=sys.stderr)
    return Result(reason=f"unknown lane {lane!r}")

  now = time.time()
  state = load_state(state_path)
  window = cooldown_seconds(resolved_lane, cooldown)
  if not should_send(state, dedup_key, window, now):
    entry = state.cooldowns.setdefault(dedup_key or "", {})
    entry["suppressed"] = int(entry.get("suppressed") or 0) + 1
    entry["lane"] = resolved_lane.value
    state.suppressed_total += 1
    save_state(state, state_path)
    return Result(suppressed=True, reason=f"within {window / 60.0:.0f}min cooldown")

  msg = build_message(
    resolved_lane,
    title,
    message,
    priority=priority,
    tags=tags,
    click=click,
    markdown=markdown,
    delay=delay,
    dedup_key=dedup_key,
  )
  ok, reason = publish(msg)
  if ok and dedup_key:
    entry = state.cooldowns.setdefault(dedup_key, {})
    entry["last_sent"] = now
    entry["lane"] = resolved_lane.value
    entry.setdefault("suppressed", 0)
    save_state(state, state_path)
  return Result(sent=ok, reason=reason)


def resolved(
  key: str,
  message: str,
  *,
  title: str | None = None,
  state_path: Path | None = None,
) -> Result:
  """Announce that a previously-alerted condition has cleared.

  Always `nas-infra` at priority 2. A recovery at high priority is how an
  alerting system teaches you to swipe it away: the phone buzzes for good news
  exactly as hard as for bad, so both stop meaning anything.
  """
  return notify(
    Lane.INFRA,
    title or f"RESOLVED: {key}",
    message,
    priority=2,
    tags=(TAG_RESOLVED,),
    state_path=state_path,
  )


def transition(
  key: str,
  *,
  active: bool,
  lane: Lane | str,
  title: str,
  message: str,
  fingerprint: str | None = None,
  resolved_message: str | None = None,
  state_path: Path | None = None,
  **kwargs,
) -> Result:
  """Publish only when `key` changes state. The `*/5` poison antidote.

  A five-minute job that pushes whenever a condition *is* true sends the same
  message 288 times a day; the phone learns to ignore the topic in about two.
  This pushes on the edge instead:

  * inactive -> active: one message on `lane`
  * active   -> active: nothing (unless `fingerprint` changed, i.e. the *detail*
    moved — "3 indexers down" becoming "5 indexers down" is news)
  * active   -> inactive: one `nas-infra` message at priority 2, tagged
    white_check_mark

  `fingerprint` is deliberately separate from `key`: the key is the identity of
  the condition and must be stable across runs, or the resolve can never match
  the alert.
  """
  state = load_state(state_path)
  prior = state.conditions.get(key) or {}
  was_active = bool(prior.get("active"))
  prior_print = str(prior.get("fingerprint") or "")
  now = time.time()

  if active:
    changed = (not was_active) or (fingerprint is not None and fingerprint != prior_print)
    if not changed:
      return Result(suppressed=True, reason="no state change")
    result = notify(lane, title, message, state_path=state_path, **kwargs)
    # Re-load: notify() rewrote the file underneath us if it touched a cooldown.
    state = load_state(state_path)
    state.conditions[key] = {
      "active": True,
      "changed_at": now,
      "since": prior.get("since") or now,
      "fingerprint": fingerprint or "",
    }
    save_state(state, state_path)
    return result

  if not was_active:
    return Result(suppressed=True, reason="no state change")
  since = float(prior.get("since") or now)
  text = resolved_message or f"cleared after {(now - since) / 60.0:.0f} min"
  result = resolved(key, text, state_path=state_path)
  state = load_state(state_path)
  state.conditions[key] = {"active": False, "changed_at": now, "fingerprint": ""}
  save_state(state, state_path)
  return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Publish one notification through the lane router.",
  )
  parser.add_argument(
    "--lane",
    required=True,
    help="One of: " + ", ".join(lane.value for lane in Lane),
  )
  parser.add_argument("--title", required=True, help="X-Title.")
  parser.add_argument("--message", required=True, help="Message body.")
  parser.add_argument("--priority", type=int, default=None, help="Override the lane's priority.")
  parser.add_argument("--tags", default="", help="Comma-separated ntfy tags.")
  parser.add_argument("--click", default=None, help="X-Click URL.")
  parser.add_argument("--markdown", action="store_true", help="Render the body as markdown.")
  parser.add_argument("--delay", default=None, help="X-Delay value (overrides quiet hours).")
  parser.add_argument("--dedup-key", default=None, help="Cooldown identity for this message.")
  parser.add_argument("--cooldown", type=float, default=None, help="Cooldown in MINUTES.")
  parser.add_argument(
    "--print-lanes",
    action="store_true",
    help="Print the lane -> topic/priority table and exit.",
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  if argv is None:
    argv = sys.argv[1:]
  if "--print-lanes" in argv:
    for lane, spec in LANES.items():
      cd = "-" if spec.default_cooldown_min is None else f"{spec.default_cooldown_min:.0f}min"
      print(f"{lane.value:10} {topic_for(lane):14} prio={spec.priority} "
            f"delayable={str(spec.delayable):5} cooldown={cd}")
    return 0

  args = parse_args(argv)
  try:
    lane = lane_of(args.lane)
  except ValueError:
    print(f"ERROR: unknown lane {args.lane!r}; one of "
          f"{', '.join(x.value for x in Lane)}", file=sys.stderr)
    return 2
  if not args.title.strip() or not args.message.strip():
    print("ERROR: --title and --message must both be non-empty", file=sys.stderr)
    return 2

  result = notify(
    lane,
    args.title,
    args.message,
    priority=args.priority,
    tags=tuple(t.strip() for t in args.tags.split(",") if t.strip()),
    click=args.click,
    markdown=args.markdown,
    delay=args.delay,
    dedup_key=args.dedup_key,
    cooldown=args.cooldown,
  )
  if result.sent:
    print(f"sent to {topic_for(lane)} ({result.reason})")
    return 0
  if result.suppressed:
    print(f"suppressed for {topic_for(lane)}: {result.reason}")
    return 0
  print(f"NOT sent to {topic_for(lane)}: {result.reason}", file=sys.stderr)
  return 1


if __name__ == "__main__":
  sys.exit(main())
