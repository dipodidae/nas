#!/usr/bin/env python3
"""Interactive helper to add a local development project (with a Dockerfile)
to the stack's compose files.

Features:
  * Scans a projects directory (default: ~/projects or $PROJECTS_DIRECTORY)
  * Detects Dockerfiles and parses EXPOSE lines (multi-port aware)
  * Shows existing host ports already claimed anywhere in the stack
  * Proposes host port mappings (uses container port when free, else finds next free)
  * Lets you interactively select a project and confirm before writing
  * Appends a minimally opinionated service block (no reformat of existing file)
  * Adds label `swag=enable` for reverse proxy auto-discovery
  * Optional custom subdomain override
  * Output passes scripts/check-invariants.sh: it extends the svc-hardened-tz
    fragment (hardening + capped logging), binds ports to 127.0.0.1, and is
    deliberately unlabeled for Watchtower because a locally-built image cannot
    be pulled (docs/decisions/0006-watchtower-opt-outs.md)

Safety:
  * Only appends – never rewrites existing service definitions
  * Refuses to add if a service with the same name already exists

Environment Variables:
  PROJECTS_DIRECTORY   Override project scan root (default: ~/projects)
  PUBLIC_DOMAIN               Used to display expected https://<service>.<domain> URL (if set)

Target file:
  The stack is split across compose.yaml + compose/*.yaml + webapps/*/compose.yaml
  (docs/decisions/0000-compose-layout.md), so there is no single file to append
  to. Ad-hoc local projects go to compose/local-projects.yaml, which this script
  creates and wires into compose.yaml's `include:` list on first use. Override
  with --module.

Usage:
  python scripts/project_service_adder.py              # interactive
  python scripts/project_service_adder.py --list       # list detected candidate projects
  python scripts/project_service_adder.py --project foo --yes  # non-interactive add
  python scripts/project_service_adder.py --project foo --subdomain api --yes

Design Notes:
  Keeps logic pure & testable; side-effect functions isolated (file write, input).
  Avoids extra dependencies (simple text UI). PyYAML already available but NOT used
  to rewrite compose (we only parse ports; raw append preserves comments & ordering).
"""
from __future__ import annotations

import argparse
import contextlib
import os
import random
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT_COMPOSE = Path("compose.yaml")
DEFAULT_MODULE = Path("compose/local-projects.yaml")
FRAGMENTS = "_fragments.yaml"
DEFAULT_NETWORK = "nas-network"

NEW_MODULE_HEADER = """\
# Ad-hoc local development projects, added by scripts/project_service_adder.py.
#
# Separate module because these have their own blast radius: they are
# locally-built, experimental, and losing one costs nothing. Nothing in the
# rest of the stack should ever depend_on a service in here.
#
# All of them are deliberately unlabeled for Watchtower -- it cannot pull a
# locally-built image. docs/decisions/0006-watchtower-opt-outs.md

services:
"""


def all_compose_files() -> list[Path]:
    """Every file that can define a service, for collision checks."""
    return [
        *sorted(Path("compose").glob("*.yaml")),
        *sorted(Path("webapps").glob("*/compose.yaml")),
    ]


def merged_compose_text() -> str:
    """Concatenated text of every compose module (ports/name collision scan)."""
    return "\n".join(f.read_text(encoding="utf-8") for f in all_compose_files())


@dataclass
class ProjectCandidate:
    name: str
    path: Path
    dockerfile: Path
    exposed: list[str]  # raw container ports (may include protocol suffix like 8080/tcp)


def expand_projects_root() -> Path:
    root = os.getenv("PROJECTS_DIRECTORY") or "~/projects"
    return Path(os.path.expanduser(root)).resolve()


def find_projects(root: Path) -> list[ProjectCandidate]:
    candidates: list[ProjectCandidate] = []
    if not root.exists():
        return candidates
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        dockerfile = child / "Dockerfile"
        if dockerfile.is_file():
            exposed = parse_expose_ports(dockerfile.read_text(encoding="utf-8", errors="ignore"))
            candidates.append(ProjectCandidate(name=child.name, path=child, dockerfile=dockerfile, exposed=exposed))
    return candidates


EXPOSE_RE = re.compile(r"^\s*EXPOSE\s+(.+)$", re.IGNORECASE | re.MULTILINE)


def parse_expose_ports(dockerfile_text: str) -> list[str]:
    ports: list[str] = [
        token
        for match in EXPOSE_RE.finditer(dockerfile_text)
        for token in (t.strip() for t in match.group(1).strip().split())
        if token and re.match(r"^\d+(/(tcp|udp))?$", token)
    ]
    return ports


def parse_existing_host_ports(compose_text: str) -> set[int]:
    host_ports: set[int] = set()
    # Match '- 8080:8080', '- "8080:80"', and the loopback form this stack uses
    # almost everywhere: "- '127.0.0.1:8080:8080'". The optional host-IP group
    # matters -- without it every 127.0.0.1-bound publish is invisible here and
    # the tool happily proposes a port that is already taken.
    port_line_re = re.compile(
        r"^[ \t-]+['\"]?(?:(?:\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-fA-F:]+\]):)?(\d+):(\d+)",
        re.MULTILINE,
    )
    for m in port_line_re.finditer(compose_text):
        with contextlib.suppress(ValueError):
            host_ports.add(int(m.group(1)))
    return host_ports


def service_name_exists(compose_text: str, name: str) -> bool:
    # Naive but sufficient: line starting at two spaces then name+':'
    pattern = re.compile(rf"^\s{{2,}}{re.escape(name)}:\s*$", re.MULTILINE)
    return bool(pattern.search(compose_text))


def propose_port(container_port: int, used: set[int]) -> int:
    if container_port not in used and container_port >= 1024:
        return container_port
    # If privileged (<1024) or taken, search upward
    p = max(container_port, 1024)
    while p in used:
        p += 1
        if p > 65000:  # improbable
            p = random.randint(20000, 40000)
    return p


def build_service_block(
    name: str,
    project_path: Path,
    port_map: list[tuple[int, str]],  # (host_port, container_port_spec)
    subdomain: str | None,
    add_basic_env: bool,
) -> str:
    lines: list[str] = []
    lines.append(f"  {name}:")
    # Inherit the hardening baseline (no-new-privileges, cap_drop ALL, capped
    # json-file logging, restart policy, TZ) instead of restating it -- see
    # docs/decisions/0000-compose-layout.md.
    lines.append("    extends:")
    lines.append(f"      file: {FRAGMENTS}")
    lines.append("      service: svc-hardened-tz")
    lines.append("    build:")
    # Represent build context via PROJECTS_DIRECTORY env var if project lives under it
    proj_root = expand_projects_root()
    try:
        rel = project_path.relative_to(proj_root)
        context_expr = f"${{PROJECTS_DIRECTORY:-{proj_root}}}/{rel}"  # env overrideable
    except ValueError:
        # Not under the root; fall back to absolute (user can adjust manually)
        context_expr = str(project_path)
    lines.append(f"      context: {context_expr}")
    lines.append("      dockerfile: Dockerfile")
    lines.append(f"    image: nas/{name}:latest")
    lines.append("    pull_policy: build")
    lines.append(f"    container_name: {name}")
    if add_basic_env:
        lines.append("    environment:")
        lines.append("      - PUID=${PUID:-1000}")
        lines.append("      - PGID=${PGID:-1000}")
        # TZ comes from the svc-hardened-tz fragment.
    if port_map:
        lines.append("    ports:")
        for host_port, container_spec in port_map:
            # 127.0.0.1 on purpose: the public surface is SWAG, which reaches
            # this container over nas-network. ADR-0001, enforced by
            # scripts/check-invariants.sh.
            lines.append(f"      - '127.0.0.1:{host_port}:{container_spec}'")
    lines.append("    networks:")
    lines.append(f"      - {DEFAULT_NETWORK}")
    lines.append("    labels:")
    # No watchtower label: a locally-built image cannot be pulled, so the label
    # buys nothing and adds recreate risk. ADR-0006.
    lines.append("      - swag=enable")
    # Healthcheck: use first TCP port if any
    first_tcp = next((c for _, c in port_map if not c.endswith("/udp")), None)
    if first_tcp:
        port_only = first_tcp.split("/")[0]
        lines.append("    healthcheck:")
        lines.append(f"      test: [CMD, curl, -f, 'http://localhost:{port_only}/']")
        lines.append("      interval: 45s")
        lines.append("      timeout: 6s")
        lines.append("      retries: 3")
        lines.append("      start_period: 30s")
    # Comment for proxy URL
    domain = os.getenv("PUBLIC_DOMAIN")
    if domain:
        sd = subdomain or name
        lines.append(f"    # Expected URL: https://{sd}.{domain}")
    return "\n".join(lines)


def interactive_select(candidates: list[ProjectCandidate]) -> ProjectCandidate | None:
    if not candidates:
        print("No Dockerfile projects found.")
        return None
    print("Detected projects:\n")
    for idx, c in enumerate(candidates, 1):
        ports = ",".join(c.exposed) if c.exposed else "(no EXPOSE)"
        print(f"  [{idx}] {c.name}  {ports}")
    print("  [0] Cancel")
    while True:
        choice = input("Select a project number: ").strip()
        if choice == "0":
            return None
        if choice.isdigit():
            i = int(choice)
            if 1 <= i <= len(candidates):
                return candidates[i - 1]
        print("Invalid selection. Try again.")


def ensure_compose_exists() -> str:
    if not ROOT_COMPOSE.is_file():
        print(f"❌ {ROOT_COMPOSE} not found. Run this from the repo root.")
        sys.exit(2)
    files = all_compose_files()
    if not files:
        print("❌ no compose modules found under compose/ or webapps/*/.")
        sys.exit(2)
    return merged_compose_text()


def ensure_module(module: Path) -> None:
    """Create the target module and wire it into `include:` if it is new."""
    if not module.is_file():
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text(NEW_MODULE_HEADER, encoding="utf-8")
        print(f"📄 created {module}")

    root_text = ROOT_COMPOSE.read_text(encoding="utf-8")
    include_line = f"  - {module.as_posix()}"
    if include_line in root_text:
        return
    if "\ninclude:" not in root_text and not root_text.startswith("include:"):
        print(f"⚠️  {ROOT_COMPOSE} has no `include:` list; add this line by hand:")
        print(include_line)
        return
    with ROOT_COMPOSE.open("a", encoding="utf-8") as f:
        if not root_text.endswith("\n"):
            f.write("\n")
        f.write("\n  # ad-hoc local projects (scripts/project_service_adder.py)\n")
        f.write(include_line + "\n")
    print(f"🔗 added {module} to {ROOT_COMPOSE} include list")


def append_service_block(module: Path, service_block: str, name: str) -> None:
    # Use timezone-aware UTC datetime (avoid deprecated utcnow())
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    existing = module.read_text(encoding="utf-8") if module.is_file() else ""
    marker = f"\n# --- added by project_service_adder {ts} ({name}) ---\n"
    if existing and not existing.endswith("\n"):
        marker = "\n" + marker
    with module.open("a", encoding="utf-8") as f:
        f.write(marker)
        f.write(service_block)
        f.write("\n")


def choose_subdomain(default: str) -> str | None:
    val = input(f"Subdomain (blank = use '{default}'): ").strip()
    return val or None


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Add a local project service to the stack's compose files"
    )
    parser.add_argument(
        "--module",
        type=Path,
        default=DEFAULT_MODULE,
        help=f"Compose module to append to (default: {DEFAULT_MODULE})",
    )
    parser.add_argument("--project", help="Project directory name (non-interactive)")
    parser.add_argument("--yes", action="store_true", help="Auto-confirm addition")
    parser.add_argument("--list", action="store_true", help="List detected projects and exit")
    parser.add_argument("--subdomain", help="Override subdomain for reverse proxy")
    parser.add_argument("--no-env", action="store_true", help="Do not add basic PUID/PGID/TZ env block")
    args = parser.parse_args(list(argv) if argv is not None else None)

    compose_text = ensure_compose_exists()
    used_ports = parse_existing_host_ports(compose_text)

    root = expand_projects_root()
    candidates = find_projects(root)
    if args.list:
        for c in candidates:
            ports = ", ".join(c.exposed) if c.exposed else "(no EXPOSE)"
            print(f"{c.name}\t{ports}")
        return 0

    selected: ProjectCandidate | None = None
    if args.project:
        selected = next((c for c in candidates if c.name == args.project), None)
        if not selected:
            print(f"❌ Project '{args.project}' not found under {root}")
            return 1
    else:
        selected = interactive_select(candidates)
        if not selected:
            print("Aborted.")
            return 0

    name = selected.name
    if service_name_exists(compose_text, name):
        print(f"❌ A service named '{name}' already exists in the compose files")
        return 1

    # Determine port mappings
    port_map: list[tuple[int, str]] = []
    for raw in selected.exposed:
        # raw like '8080' or '8080/udp'
        parts = raw.split("/")
        cport = int(parts[0])
        proto_suffix = "" if len(parts) == 1 or parts[1] == "tcp" else "/udp"
        host_port = propose_port(cport, used_ports)
        used_ports.add(host_port)
        port_map.append((host_port, f"{cport}{proto_suffix}"))

    # Optional subdomain input if not provided
    subdomain = args.subdomain
    if subdomain is None and sys.stdin.isatty() and not args.yes:
        subdomain = choose_subdomain(name)

    service_block = build_service_block(
        name=name,
        project_path=selected.path,
        port_map=port_map,
        subdomain=subdomain,
        add_basic_env=not args.no_env,
    )

    print("\nProposed service block:\n")
    print(service_block)
    print()
    if os.getenv("PUBLIC_DOMAIN"):
        final_sd = subdomain or name
        print(f"Reverse proxy target will become: https://{final_sd}.{os.getenv('PUBLIC_DOMAIN')} (label swag=enable)")

    if not args.yes:
        confirm = input(f"Add this service to {args.module}? [y/N]: ").strip().lower()
        if confirm not in {"y", "yes"}:
            print("Cancelled.")
            return 0

    ensure_module(args.module)
    append_service_block(args.module, service_block, name)
    print(f"✅ Service '{name}' appended to {args.module}")
    print("Verify before starting it:")
    print("  make lint && make check")
    print(f"Then: docker compose up -d --build {name}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
