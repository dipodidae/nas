# Custom Instructions (Short Form)

Short, self-contained statements injected into Copilot Chat. Do not alter answer style/length unless user explicitly asks.

Project & Infra

- Single Docker Compose file; new services join network `nas-network` and include a lightweight `curl -f` (or equivalent) healthcheck.
- Use linuxserver.io images where precedent exists; justify alternatives in a comment.
- Map configs under `${CONFIG_DIRECTORY}/<service>`; never hard-code user paths or secrets.
- Expose services via SWAG by adding label `swag=enable`; otherwise keep internal.
- Only labeled containers auto-update (Watchtower). Explain any version pin or digest.
- Do not request privileged mode, host networking, or extra capabilities without justification.
- Access Docker only through existing `dockerproxy`; never mount raw `/var/run/docker.sock`.

Security & Secrets

- Never commit secrets; use env vars. If adding a required env var, also request README update.
- Do not output the real username or absolute home directory; refer to env vars instead.

Python Scripts (`scripts/`)

- Small, focused functions (one responsibility); avoid boolean flag parameters—split functions instead.
- Keep side effects (filesystem, network) thin and centralized in `main()`; core logic should be pure and testable.
- Favor meaningful names and constants over magic numbers; prefer dataclass / simple object to long param lists.
- Catch narrow exceptions; only broad catch at top-level for clean exit code and context-rich error message.

Shell Snippets

- Start with `#!/usr/bin/env bash` + `set -euo pipefail` + `IFS=$'\n\t'` when creating a new script.
- Quote variable expansions and prefer arrays for argument lists; avoid `eval`.

General Guidance

- Readability and maintainability first—optimize only after measurement.
- Avoid introducing heavy new dependencies for trivial tasks; propose before adding.
- Provide actionable error messages (include key identifiers like path, service name, counts).
- Update or add a healthcheck when adding a service or changing its main port.
- If unsure about a structural change, propose a short plan before editing.

Out of Scope

- Do not change overall response tone/style or impose arbitrary length limits.
- Do not reference external private resources.

See `copilot-instructions.extensive.md` in the same directory for the comprehensive project guide.
