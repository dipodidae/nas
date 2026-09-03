#!/usr/bin/env python3
"""Fire one representative message per lane, then prove each one arrived.

Why the read-back exists
------------------------
A `200` from ntfy is not delivery. This repo has been bitten three times by a
call that returned success and did nothing — the \\*arr "Update Library"
connection answering `204` for a path under no Jellyfin library, the \\*arr Test
button answering `200` while exercising an API Jellyfin does not implement, and
bazarr's post-processing reporting a downloaded subtitle it had discarded.
AGENTS.md states the rule: when a check passes, ask whether it proves the
property you care about or just the component that carries it.

So this publishes with the write-only `nas-scripts` token and then reads every
message back out of ntfy's own cache with the **read-only `nas-phone`** token —
the same credential the phone uses. That proves three things at once: the
publish landed, the message is in the topic a subscriber actually reads, and the
phone's own credential can see it.

`nas-critical` is deliberately included. It is the lane whose delivery matters
most and therefore the one most worth proving; every message says it is a test
in its own title, so a phone that buzzes is not misleading.

Exit codes
----------
  0  every lane published AND was read back
  1  at least one lane did not arrive
  2  fatal (no token configured)

Usage
-----
  make notify-test
  python scripts/notify_test.py --lanes critical,media
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if "NTFY_TOKEN_SCRIPTS" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify as notifier  # noqa: E402, I001


# One message per lane that looks like the real thing it stands for, so the
# phone's per-topic settings can be judged from it.
SAMPLES: dict[notifier.Lane, tuple[str, str, tuple[str, ...]]] = {
  notifier.Lane.CRITICAL: (
    "TEST nas-critical — service has no container",
    "This is `make notify-test`. A real message here means a compose service "
    "exists in the model but has no container at all, an OOM kill, a disk "
    "error, a failed config backup, or a user-visible service down >5 min.",
    (notifier.TAG_CRITICAL,),
  ),
  notifier.Lane.ATTENTION: (
    "TEST nas-attention — needs a human today",
    "This is `make notify-test`. A real message here means an *arr health "
    "issue, a manual interaction required, an import or download failure, "
    "slskd logged out past its grace, disk >90%, or cleanuparr deleting "
    "something.",
    (notifier.TAG_ATTENTION,),
  ),
  notifier.Lane.MEDIA: (
    "TEST nas-media — 📺 Something You Can Watch S01E01",
    "This is `make notify-test`. A real message here is an episode, movie or "
    "album that finished importing, with its quality and size.",
    (notifier.TAG_TV,),
  ),
  notifier.Lane.REQUESTS: (
    "TEST nas-requests — request pending approval",
    "This is `make notify-test`. A real message here is a Jellyseerr request "
    "waiting on you, a declined or failed request, or an issue someone "
    "reported.",
    (notifier.TAG_REQUEST,),
  ),
  notifier.Lane.INFRA: (
    "TEST nas-infra — routine ops",
    "This is `make notify-test`. A real message here is an autoheal restart, a "
    "container recovering, a first cron failure, or the 09:00 digest.",
    (notifier.TAG_INFRA,),
  ),
  notifier.Lane.UPDATES: (
    "TEST nas-updates — image update available",
    "This is `make notify-test`. A real message here is diun reporting a newer "
    "image tag. Nothing is broken and nothing is waiting; a human applies it.",
    (notifier.TAG_UPDATE,),
  ),
}
READ_BACK_WINDOW = "5m"
# ntfy writes to its cache synchronously, but give the round trip a moment so a
# slow box does not read before the write lands.
SETTLE_S = 1.5


def read_back(topic: str, token: str, window: str = READ_BACK_WINDOW) -> list[dict]:
  """Every cached message in `topic`, via the READ-ONLY phone token."""
  url = f"{os.getenv('NTFY_URL') or notifier.DEFAULT_URL}/{topic}/json?poll=1&since={window}"
  req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
  try:
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - loopback
      raw = resp.read().decode("utf-8", "replace")
  except (OSError, urllib.error.HTTPError, ValueError) as exc:
    print(f"    !!! could not read {topic} back: {exc}", file=sys.stderr)
    return []
  out = []
  for line in raw.splitlines():
    if not line.strip():
      continue
    try:
      msg = json.loads(line)
    except ValueError:
      continue
    if msg.get("event") == "message":
      out.append(msg)
  return out


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="Fire and verify one message per lane.")
  parser.add_argument(
    "--lanes",
    default="",
    help="Comma-separated lanes to test (default: all six).",
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  if not os.getenv("NTFY_TOKEN_SCRIPTS"):
    print("ERROR: NTFY_TOKEN_SCRIPTS is not set; nothing can be published",
          file=sys.stderr)
    return 2
  phone = os.getenv("NTFY_TOKEN_PHONE", "")
  if not phone:
    print("WARNING: NTFY_TOKEN_PHONE unset — publishing, but a 200 will not be "
          "verified as delivery", file=sys.stderr)

  wanted = [x.strip() for x in args.lanes.split(",") if x.strip()]
  lanes = list(SAMPLES)
  if wanted:
    try:
      lanes = [notifier.lane_of(x) for x in wanted]
    except ValueError as exc:
      print(f"ERROR: {exc}", file=sys.stderr)
      return 2

  rc = 0
  published: dict[notifier.Lane, str] = {}
  for lane in lanes:
    title, body, tags = SAMPLES[lane]
    # No delay, whatever the hour: `make notify-test` is run BY a human who is
    # watching their phone, so holding the chatter lanes until 08:00 would make
    # the target useless exactly when it is used.
    result = notifier.notify(lane, title, body, tags=tags, markdown=True, delay="")
    topic = notifier.topic_for(lane)
    if result.sent:
      print(f"    published  nas-{lane.value:9} -> {topic}")
      published[lane] = title
    else:
      print(f"    !!! FAILED nas-{lane.value:9} -> {topic}: {result.reason}")
      rc = 1

  if not phone or not published:
    return rc

  time.sleep(SETTLE_S)
  print()
  print("    reading back with the READ-ONLY nas-phone token "
        "(a 200 on publish is not delivery):")
  for lane, title in published.items():
    topic = notifier.topic_for(lane)
    titles = [m.get("title") for m in read_back(topic, phone)]
    if title in titles:
      print(f"    ok         nas-{lane.value:9} message is in the topic")
    else:
      print(f"    !!! MISSING nas-{lane.value:9} published but not readable in {topic}")
      rc = 1
  return rc


if __name__ == "__main__":
  sys.exit(main())
