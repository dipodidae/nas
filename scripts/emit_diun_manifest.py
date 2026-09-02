#!/usr/bin/env python3
"""Emit Diun's file-provider manifest from the live compose model.

Why a generated manifest rather than a hand-maintained one, and rather than
Diun's docker provider:

* The **docker provider** reads the tag a container was *started from*, which is
  exactly watchtower's blind spot (ADR-0020): a pinned tag never reports an
  update, so the two services whose updates most need to be chosen deliberately
  -- `jellyfin` and `qbittorrent` -- get no notification at all. The **file
  provider** watches a repository and enumerates its tags, which is what makes a
  pin visible.
* A hand-maintained YAML list drifts the moment a service is added, and drift in
  a *notification* config is silent by construction: nothing tells you that the
  thing that was supposed to tell you has stopped covering something. So the
  manifest is derived from `docker compose config` -- one source of truth -- and
  `scripts/check-invariants.sh` asserts the tracked file still matches what this
  emitter produces.

Locally-built services are skipped: they have no registry to watch, which is the
same derived opt-out `check-invariants.sh` already applies to watchtower labels.

Exit codes
----------
  0  manifest written (or already current with --check)
  1  --check and the tracked manifest is out of date
  2  fatal (compose model unreadable)

Usage
-----
  python scripts/emit_diun_manifest.py                 # write diun/manifest.yml
  python scripts/emit_diun_manifest.py --check         # is the tracked file current?
  python scripts/emit_diun_manifest.py --stdout        # print, write nothing
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_OUT = Path("diun/manifest.yml")

# Tags that are moving pointers rather than releases. Watching a repo and then
# ranking these by semver produces noise and, worse, ranks them ABOVE real
# releases in some registries.
GLOBAL_EXCLUDE = [
    "^latest$",
    "^nightly$",
    "^develop(ment)?$",
    "^edge$",
    "^main$",
    "^master$",
    "^unstable$",
    "^beta$",
    "^rc",
    "-rc",
    "^dev$",
    "-dev$",
]


@dataclass(frozen=True)
class Policy:
    """Per-image watch policy. `include`/`exclude` are Diun tag regexes."""

    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    max_tags: int = 5
    note: str = ""


# Only images whose tag is PINNED need a policy: those are the ones where the
# question is "what newer tags exist", so Diun has to enumerate and rank the
# repository. A `:latest` image needs none -- Diun watches that one tag and
# reports when the digest behind it moves, which is the same signal watchtower
# gave, minus the ability to act on it.
#
# Keyed by image repository (registry/path), tag stripped.
POLICIES: dict[str, Policy] = {
    "lscr.io/linuxserver/qbittorrent": Policy(
        # linuxserver release tags look like 5.2.3_v2.0.14-ls473.
        include=[r"^\d+\.\d+\.\d+_v\d+\.\d+\.\d+-ls\d+$"],
        # ADR-0005: the floor is qBittorrent >= 5.2.2 (upstream #24357, fixed by
        # #24363). Anything below it is not an upgrade this stack may take, so it
        # must not be offered -- a notification suggesting a version the
        # invariants forbid is worse than none.
        exclude=[r"^[0-4]\.", r"^5\.[01]\.", r"^5\.2\.[01]_"],
        note="floor >= 5.2.2 per ADR-0005; LSIO release tags only",
    ),
    "lscr.io/linuxserver/jellyfin": Policy(
        # e.g. 10.11.11ubu2604-ls47. semver sorting puts the newest first, so a
        # major bump surfaces at the top of the same list as a point release --
        # see ADR-0024 for why this is one entry and not two.
        include=[r"^\d+\.\d+\.\d+ubu\d+-ls\d+$"],
        note="release tags only; a 10.11.z point release and a major both surface",
    ),
    "ghcr.io/analogj/scrutiny": Policy(
        include=[r"^v\d+\.\d+\.\d+-omnibus$"],
        note="omnibus variant only -- the split images are a different topology",
    ),
    "crazymax/diun": Policy(
        include=[r"^\d+\.\d+\.\d+$"],
        note="plain semver tags",
    ),
}


def compose_model() -> dict:
    """The fully-resolved compose model. Raises RuntimeError if unreadable."""
    try:
        raw = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            capture_output=True, text=True, check=True,
        ).stdout
        return json.loads(raw)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not render the compose model: {exc}") from exc


def split_image(image: str) -> tuple[str, str]:
    """Split an image ref into (repository, tag). Pure.

    Handles a registry port and a digest pin, neither of which a bare
    rpartition(':') survives.
    """
    if "@" in image:
        repo, _, digest = image.partition("@")
        return repo.rsplit(":", 1)[0] if ":" in repo.rsplit("/", 1)[-1] else repo, digest
    head, _, last = image.rpartition("/")
    if ":" in last:
        name, _, tag = last.rpartition(":")
        return (f"{head}/{name}" if head else name), tag
    return image, "latest"


def watched_services(services: dict) -> list[tuple[str, str]]:
    """[(service, image)] for every service Diun can actually watch. Pure.

    Locally-built services are excluded: no registry, nothing to watch. That
    mirrors the derived watchtower opt-out in check-invariants.sh, so adding a
    new local project needs no edit here either.
    """
    out = []
    for name, svc in sorted(services.items()):
        image = svc.get("image")
        if not image or "build" in svc:
            continue
        out.append((name, image))
    return out


def render(entries: list[tuple[str, str]]) -> str:
    """Render the Diun file-provider manifest. Pure -- no I/O, so it is diffable.

    Emitted by hand rather than through a YAML library so the output is stable,
    commented, and reviewable in a diff. The header names the emitter, because a
    generated file that does not say so gets hand-edited.
    """
    lines = [
        "# GENERATED by scripts/emit_diun_manifest.py -- DO NOT EDIT BY HAND.",
        "#",
        "# Diun's file-provider manifest: what to watch for new image versions.",
        "# Regenerate with `make diun-manifest`; `make check` asserts this file",
        "# still matches the compose model, because drift in a notification",
        "# config is silent -- nothing tells you the thing meant to tell you has",
        "# stopped covering something.",
        "#",
        "# Locally-built services are absent on purpose: no registry to watch.",
        "# See docs/decisions/0024-diun-version-aware-notification.md",
        "",
    ]
    for service, image in entries:
        repo, tag = split_image(image)
        policy = POLICIES.get(repo)
        lines.append(f"# {service}")
        if policy and policy.note:
            lines.append(f"#   {policy.note}")
        lines.append(f"- name: {image}")
        lines.append("  metadata:")
        lines.append(f"    service: {service}")
        if policy:
            # A pinned tag: enumerate and rank the repository, because the whole
            # point is to see versions NEWER than the pin.
            lines.append("  watch_repo: true")
            lines.append("  sort_tags: semver")
            lines.append(f"  max_tags: {policy.max_tags}")
            if policy.include:
                lines.append("  include_tags:")
                lines.extend(f"    - '{rx}'" for rx in policy.include)
            excludes = policy.exclude + GLOBAL_EXCLUDE
            lines.append("  exclude_tags:")
            lines.extend(f"    - '{rx}'" for rx in excludes)
        else:
            # A moving tag (:latest / :nightly): watch that one tag and report
            # when the digest behind it changes. Enumerating the repo here would
            # be pure noise.
            lines.append("  watch_repo: false")
        lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the tracked manifest is out of date")
    ap.add_argument("--stdout", action="store_true", help="print instead of writing")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        model = compose_model()
    except RuntimeError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    entries = watched_services(model.get("services") or {})
    if not entries:
        print("FATAL: no watchable services in the compose model", file=sys.stderr)
        return 2
    rendered = render(entries)

    if args.stdout:
        print(rendered, end="")
        return 0

    if args.check:
        try:
            current = args.out.read_text()
        except OSError:
            print(f"{args.out} is missing -- run `make diun-manifest`", file=sys.stderr)
            return 1
        if current != rendered:
            print(
                f"{args.out} is out of date with the compose model. "
                "Run `make diun-manifest` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"{args.out}: current ({len(entries)} images)")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered)
    print(f"wrote {args.out}: {len(entries)} images")
    return 0


if __name__ == "__main__":
    sys.exit(main())
