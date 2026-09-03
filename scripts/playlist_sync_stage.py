#!/usr/bin/env python3
"""Run ONE stage of the playlist-generator pipeline, in a bounded batch.

Why this exists
---------------
`cron-sync.sh` drove the whole pipeline through a single
`POST /sync/full-pipeline` SSE stream. That cannot work here, and the numbers
say why rather than the design:

* One pass is measured at **~1,900 tracks/h** for the Last.fm track stage alone
  (10,500 -> 17,400 of 144,756 between 11:06 and 14:44 on 2026-09-03), so the
  remaining backlog is ~67 hours for that ONE stage, before Metal Archives,
  embeddings, profiles, clustering, audio and search vectors.
* The cron line ran every 6h with `--max-age-min 1440`. A job needing 3+ days
  per pass, scheduled 6-hourly and judged stale after 24h, can never report
  success -- so it paged hourly forever while working correctly.
* Holding one HTTP stream open for days does not survive contact with reality.
  `logs/playlist_sync.log` records `Stream closed without a completion signal`
  on 08-24, 08-26, 08-29, 09-01 (twice), 09-02 (twice), at wildly different
  durations (100 min, 10.7h, 21h+) and at different stages. Not a timeout --
  just a long stream.

So: one stage per invocation, bounded, short. Each run finishes in minutes, the
freshness clock advances every run, and the backlog drains over days. This is
the same shape as `lidarr_backlog_drip.py` and `album_art.py --limit`, which
exist for exactly this reason.

Two things make it possible, both verified against the running backend:

* Every stage already has its own endpoint -- no submodule change was needed.
* The plain `POST /enrich/<stage>` endpoints are **fire-and-forget**: they
  return `{"status": "started"}` immediately and run in the background, so a
  cron job calling them would exit 0 without having done anything verifiable.
  The `/stream` variants block and emit a terminal `done` event, which is the
  only form whose exit code means something. Bounded batches keep those streams
  short, which is what removes the fragility -- a 30-second stream is fine, a
  three-day one is not.

The backend publishes no host port (uvicorn listens on 127.0.0.1:8000 *inside*
the container, behind nginx basic-auth), so this reaches it the same way
`cron-sync.sh` did: `docker exec ... curl`, which also bypasses SWAG.

Exit codes
----------
  0  the stage completed
  1  nothing to do, or the backend is not available right now -- a reported
     condition rather than a fault, so it does not page and the next tick
     retries (same contract as heartbeat.py)
  2  fatal: the stream broke mid-batch, an HTTP error, or the stage itself
     reported an error

Usage
-----
  python scripts/playlist_sync_stage.py --stage lastfm-tracks --limit 4000
  python scripts/playlist_sync_stage.py --stage scan
  python scripts/playlist_sync_stage.py --list
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass

CONTAINER = "playlist-generator"
INTERNAL_URL = "http://127.0.0.1:8000"


@dataclass(frozen=True)
class Stage:
  """One pipeline stage: where it lives and how (if at all) it can be bounded.

  `limit_param` is None for stages that take no batch size. Those are the ones
  that are cheap over the whole library anyway (clustering, profiles) or that
  have no bounded form to offer -- `audio` is the notable expensive exception
  and needs a schedule wide enough to absorb a full pass.
  """

  path: str
  limit_param: str | None = None
  streaming: bool = True


# Verified against the live /openapi.json. Order is pipeline order, which is the
# order the crontab should fire them in; each stage is incremental so a later
# stage running before an earlier one is merely early, not wrong.
STAGES: dict[str, Stage] = {
  "scan": Stage("/scan/stream"),
  "musicbrainz": Stage("/enrich/musicbrainz/stream"),
  "lastfm-tracks": Stage("/enrich/lastfm-tracks/stream", "max_tracks"),
  "lastfm-album-tags": Stage("/enrich/lastfm-album-tags/stream", "max_albums"),
  "metal-archives": Stage("/enrich/metal-archives/stream"),
  "release-dates": Stage("/enrich/release-dates/stream", "max_albums"),
  "embeddings": Stage("/enrich/embeddings/stream"),
  "profiles": Stage("/enrich/profiles/stream"),
  "clusters": Stage("/enrich/clusters/stream"),
  "banger-flags": Stage("/enrich/banger-flags/stream"),
  "genre-manifold": Stage("/enrich/genre-manifold/stream"),
  "audio": Stage("/enrich/audio/stream"),
  # No /stream variant exists for this one; it is a single fast rebuild.
  "search-vectors": Stage("/rebuild-search-vectors", streaming=False),
}


def build_url(stage: Stage, limit: int | None) -> str:
  """Endpoint URL with the batch bound applied, when the stage accepts one."""
  url = f"{INTERNAL_URL}{stage.path}"
  if limit is not None and stage.limit_param:
    url = f"{url}?{stage.limit_param}={limit}"
  return url


def container_available(runner=subprocess.run) -> bool:
  """True only if the container is up AND its backend answers /health.

  Both halves matter: a container that is up but still starting would make
  every stage look broken, and that is the alert-noise failure this repo keeps
  relearning (ADR-0026).
  """
  try:
    proc = runner(
      ["docker", "ps", "--format", "{{.Names}}"],
      capture_output=True, text=True, timeout=30, check=False,
    )
  except (OSError, subprocess.SubprocessError):
    return False
  if proc.returncode != 0 or CONTAINER not in proc.stdout.split():
    return False
  try:
    health = runner(
      ["docker", "exec", CONTAINER, "curl", "-sf", f"{INTERNAL_URL}/health"],
      capture_output=True, text=True, timeout=30, check=False,
    )
  except (OSError, subprocess.SubprocessError):
    return False
  return health.returncode == 0


def parse_stream(text: str) -> tuple[bool, str, dict]:
  """Read an SSE body. Returns (completed, error message, stats).

  `completed` is True only on an explicit terminal `done` event. A stream that
  simply stops is NOT a success -- that conflation is what let the old job
  report progress for weeks while finishing nothing.
  """
  done, error, stats = False, "", {}
  for line in text.splitlines():
    raw = line.strip()
    if not raw.startswith("data:"):
      continue
    try:
      event = json.loads(raw[5:].strip())
    except json.JSONDecodeError:
      continue
    if not isinstance(event, dict):
      continue
    if event.get("error"):
      error = str(event["error"])
    if event.get("done"):
      done = True
      if isinstance(event.get("stats"), dict):
        stats = event["stats"]
  return done, error, stats


def run_stage(name: str, limit: int | None, runner=subprocess.run) -> int:
  """Drive one stage. Returns this script's exit code."""
  stage = STAGES[name]
  url = build_url(stage, limit)
  print(f"stage={name} url={url}")
  try:
    proc = runner(
      ["docker", "exec", CONTAINER, "curl", "-sf", "-N", "-X", "POST", url],
      capture_output=True, text=True, check=False,
    )
  except (OSError, subprocess.SubprocessError) as exc:
    print(f"ERROR: could not invoke docker exec: {exc}", file=sys.stderr)
    return 2

  if proc.returncode != 0:
    # curl -f exits 22 on HTTP >= 400. 409 means a scan is already running,
    # which for a drip is "come back next tick", not a fault.
    if proc.returncode == 22:
      print(f"NOTE: {name} refused by the backend (likely 409: already running)")
      return 1
    print(f"ERROR: curl failed rc={proc.returncode} for {name}", file=sys.stderr)
    return 2

  if not stage.streaming:
    print(f"OK: {name} completed ({proc.stdout.strip()[:200]})")
    return 0

  done, error, stats = parse_stream(proc.stdout)
  if error:
    print(f"ERROR: {name} reported: {error}", file=sys.stderr)
    return 2
  if not done:
    print(
      f"ERROR: {name} stream closed without a completion signal — "
      f"the failure mode the bounded batches exist to avoid",
      file=sys.stderr,
    )
    return 2
  summary = ", ".join(f"{k}={v}" for k, v in sorted(stats.items())) or "no stats"
  print(f"OK: {name} completed ({summary})")
  return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Run one bounded stage of the playlist-generator pipeline.",
  )
  parser.add_argument(
    "--stage",
    nargs="+",
    choices=sorted(STAGES),
    metavar="STAGE",
    help=(
      "Pipeline stage(s) to run, in the order given. Several are accepted "
      "because the derived stages (embeddings -> profiles -> clusters -> ...) "
      "genuinely must run in order, and one cron line per stage cannot express "
      "that. Stops at the first fatal stage rather than feeding it forward."
    ),
  )
  parser.add_argument(
    "--limit",
    type=int,
    default=None,
    metavar="N",
    help=(
      "Batch size, for the stages that accept one (lastfm-tracks, "
      "lastfm-album-tags, release-dates). Ignored elsewhere."
    ),
  )
  parser.add_argument("--list", action="store_true", help="List stages and exit.")
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)

  if args.list:
    for name, stage in STAGES.items():
      bound = f"bounded by {stage.limit_param}" if stage.limit_param else "whole-library"
      print(f"  {name:<20} {stage.path:<38} {bound}")
    return 0

  if not args.stage:
    print("ERROR: --stage is required (or --list)", file=sys.stderr)
    return 2

  if shutil.which("docker") is None:
    print("ERROR: docker not found in PATH", file=sys.stderr)
    return 2

  if not container_available():
    # Not a fault: the container may be restarting or mid-deploy. Reported as a
    # condition so cron stays quiet and the next tick retries.
    print(f"NOTE: {CONTAINER} is not running or its backend is not answering /health")
    return 1

  worst = 0
  for name in args.stage:
    code = run_stage(name, args.limit)
    worst = max(worst, code)
    if code == 2:
      # Stop rather than feeding a failed stage's output into the next one.
      # A later stage is derived from an earlier one, so continuing would build
      # on data the failed stage did not produce.
      print(f"ERROR: stopping after {name} (exit 2); remaining stages skipped",
            file=sys.stderr)
      break
  return worst


if __name__ == "__main__":
  sys.exit(main())
