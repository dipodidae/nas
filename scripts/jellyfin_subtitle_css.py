#!/usr/bin/env python3
"""Widen Jellyfin's subtitle box via server Custom CSS so large text stops wrapping.

Background
----------
jellyfin-web renders subtitle cues into `.videoSubtitlesInner`, which ships with a
hardcoded `max-width: 70%`. The subtitle *size* setting scales the font but never
the box, so once the text is comfortably readable every other cue breaks onto a
second line. Jellyfin exposes no API field for the box geometry — the only
server-side lever is Branding -> Custom CSS, which every jellyfin-web client
(browser, webOS, Tizen, Fire TV) fetches on load.

This script merges a sentinel-delimited managed block into `BrandingOptions.CustomCss`:

    /* >>> nas-managed: subtitle-layout >>> */
    .videoSubtitlesInner { max-width: 92%; line-height: 1.25; padding: .1em .4em; }
    /* <<< nas-managed: subtitle-layout <<< */

Idempotent: re-running replaces the block in place rather than appending, and any
CSS outside the sentinels is preserved byte-for-byte.

Caveats
-------
- Text subtitles only (SRT/ASS). Image subs (PGS/VOBSUB) are drawn to a canvas
  and are unaffected by CSS.
- Native apps (Android/iOS/Android TV) ignore server Custom CSS entirely.
- Clients cache the CSS: hard-refresh the browser, restart the TV app.

Exit codes
----------
  0 success (or dry-run / already up to date)
  1 partial (read succeeded, write rejected)
  2 fatal (API key missing, Jellyfin unreachable, auth failed)

Environment
-----------
  API_KEY_JELLYFIN  (required) Jellyfin API key
  JELLYFIN_HOST     (default: http://localhost:8096)

Usage
-----
  python scripts/jellyfin_subtitle_css.py                    # preview (dry-run)
  python scripts/jellyfin_subtitle_css.py --apply            # write it
  python scripts/jellyfin_subtitle_css.py --apply --max-width 96 --line-height 1.2
  python scripts/jellyfin_subtitle_css.py --apply --remove   # strip the block
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

if "API_KEY_JELLYFIN" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass

DEFAULT_JELLYFIN_HOST = "http://localhost:8096"
BRANDING_PATH = "/System/Configuration/branding"

BLOCK_START = "/* >>> nas-managed: subtitle-layout >>> */"
BLOCK_END = "/* <<< nas-managed: subtitle-layout <<< */"

DEFAULT_MAX_WIDTH = 92
DEFAULT_LINE_HEIGHT = 1.25

TIMEOUT_SECONDS = 15


# ---- pure logic ----------------------------------------------------------


def render_block(max_width: int, line_height: float) -> str:
  """Render the managed CSS block for the given geometry."""
  return "\n".join(
    [
      BLOCK_START,
      ".videoSubtitlesInner {",
      f"  max-width: {max_width}%;",
      f"  line-height: {line_height};",
      "  padding: .1em .4em;",
      "}",
      BLOCK_END,
    ]
  )


def strip_block(css: str) -> str:
  """Remove the managed block (and its surrounding blank lines) from `css`."""
  start = css.find(BLOCK_START)
  if start == -1:
    return css
  end = css.find(BLOCK_END, start)
  if end == -1:
    # Truncated/hand-mangled block: drop from the start marker onward rather
    # than leaving a dangling opener that would swallow later rules.
    return css[:start].rstrip()
  remainder = css[end + len(BLOCK_END) :]
  return (css[:start].rstrip() + "\n" + remainder.lstrip()).strip()


def merge_block(css: str, block: str) -> str:
  """Insert or replace the managed block, preserving all foreign CSS."""
  foreign = strip_block(css)
  if not foreign:
    return block
  return f"{foreign}\n\n{block}"


# ---- HTTP ----------------------------------------------------------------


def _request(host: str, path: str, api_key: str, payload: dict | None = None) -> dict | None:
  """GET (payload=None) or POST JSON against Jellyfin; returns parsed JSON or None."""
  url = f"{host.rstrip('/')}{path}"
  data = None
  headers = {"X-Emby-Token": api_key, "Accept": "application/json"}
  if payload is not None:
    data = json.dumps(payload).encode("utf-8")
    headers["Content-Type"] = "application/json"
  req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
  with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
    body = resp.read().decode("utf-8").strip()
  return json.loads(body) if body else None


def fetch_branding(host: str, api_key: str) -> dict:
  """Read the current BrandingOptions."""
  branding = _request(host, BRANDING_PATH, api_key)
  if not isinstance(branding, dict):
    raise ValueError(f"unexpected branding response: {branding!r}")
  return branding


def push_branding(host: str, api_key: str, branding: dict) -> None:
  """Write BrandingOptions back whole (partial POSTs blank the omitted fields)."""
  _request(host, BRANDING_PATH, api_key, payload=branding)


# ---- entry point ---------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument(
    "--apply", action="store_true", help="write the change (default: dry-run preview)"
  )
  parser.add_argument(
    "--remove", action="store_true", help="strip the managed block instead of installing it"
  )
  parser.add_argument(
    "--max-width",
    type=int,
    default=DEFAULT_MAX_WIDTH,
    help=f"subtitle box max-width in %% (default: {DEFAULT_MAX_WIDTH})",
  )
  parser.add_argument(
    "--line-height",
    type=float,
    default=DEFAULT_LINE_HEIGHT,
    help=f"subtitle line-height (default: {DEFAULT_LINE_HEIGHT})",
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)

  api_key = os.getenv("API_KEY_JELLYFIN")
  if not api_key:
    print("ERROR: API_KEY_JELLYFIN not set (add it to .env)", file=sys.stderr)
    return 2

  if not 1 <= args.max_width <= 100:
    print(f"ERROR: --max-width must be 1-100, got {args.max_width}", file=sys.stderr)
    return 2

  host = os.getenv("JELLYFIN_HOST", DEFAULT_JELLYFIN_HOST)

  try:
    branding = fetch_branding(host, api_key)
  except urllib.error.HTTPError as exc:
    print(f"ERROR: Jellyfin rejected the branding read ({exc.code} {exc.reason})", file=sys.stderr)
    return 2
  except (urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
    print(f"ERROR: could not read branding config from {host}: {exc}", file=sys.stderr)
    return 2

  current_css = branding.get("CustomCss") or ""
  if args.remove:
    desired_css = strip_block(current_css)
    action = "Remove managed subtitle block"
  else:
    block = render_block(args.max_width, args.line_height)
    desired_css = merge_block(current_css, block)
    action = f"Set subtitle box to max-width {args.max_width}%, line-height {args.line_height}"

  if desired_css == current_css:
    print("✓ Custom CSS already matches — nothing to do")
    return 0

  print(f"{action}")
  print("--- CustomCss after ---")
  print(desired_css or "(empty)")
  print("-----------------------")

  if not args.apply:
    print("DRY-RUN: re-run with --apply to write this to Jellyfin")
    return 0

  branding["CustomCss"] = desired_css
  try:
    push_branding(host, api_key, branding)
  except urllib.error.HTTPError as exc:
    print(f"ERROR: branding update rejected ({exc.code} {exc.reason})", file=sys.stderr)
    return 1
  except urllib.error.URLError as exc:
    print(f"ERROR: branding update failed: {exc}", file=sys.stderr)
    return 1

  print("✓ Custom CSS updated")
  print("  Hard-refresh the browser (Ctrl+Shift+R); restart TV apps to re-fetch.")
  return 0


if __name__ == "__main__":
  sys.exit(main())
