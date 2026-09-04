# Custom Instructions (Short Form)

Short, self-contained statements injected into Copilot Chat. Do not alter answer style/length unless user explicitly asks.

Project & Infra

- ONE Compose project split across many files (`compose.yaml` + `compose/*.yaml` + `webapps/*/compose.yaml`, wired with `include:`) — not a single file. New services join `nas-network`, `extends` the narrowest fragment in `compose/_fragments.yaml`, and include a lightweight `curl -f` (or `wget --spider`) healthcheck that probes **nothing outside their own container**.
- Use linuxserver.io images where precedent exists; justify alternatives in a comment.
- Map configs under `${CONFIG_DIRECTORY}/<service>`; never hard-code user paths or secrets.
- What publishes a subdomain is the **conf file**, `swag/proxy-confs/<name>.subdomain.conf`, tracked in-repo and bind-mounted read-only. `swag=enable` is documentation, not mechanism (the auto-proxy mod is not installed). `make check` reconciles the two in both directions.
- Apply a conf edit with `make swag-apply`, never `nginx -s reload`: Docker binds each conf by inode, so replacing the host file (git checkout/revert, prettier, `sed -i`) detaches the mount and nginx keeps serving the old one.
- Every published route is classified `protect` or `never` in `DOOR` (`scripts/check-invariants.sh`). Browser-only routes sit behind the single **tinyauth** forward-auth door; `jellyfin`, `nextcloud` and `ntfy` must NOT, because their clients cannot follow a `302`. Never add `auth_basic` next to the forward-auth include — it preempts it rather than stacking, and locks out every valid session.
- **Watchtower is retired and nothing auto-updates.** Do NOT add a `com.centurylinklabs.watchtower.*` label — `make check` rejects one. `diun` notifies; a human applies with `docker compose pull <svc> && docker compose up -d <svc>`. Regenerate `diun/manifest.yml` with `make diun-manifest` after adding a service. Explain any version pin, and add it to `MANUAL_UPDATE_ONLY` with a reason.
- Do not request privileged mode, host networking, or extra capabilities without justification.
- Access Docker only through the `dockerproxy` service (tecnativa/docker-socket-proxy at `tcp://dockerproxy:2375`); never mount raw `/var/run/docker.sock` elsewhere. It is narrowed to `CONTAINERS`/`POST`/`PING`/`VERSION` for `autoheal` alone — do not widen it.

Security & Secrets

- Never commit secrets; use env vars. If adding a required env var, also update `.env.example` and `AGENTS.md`'s env list.
- A credential a container **reads** belongs in a `0600` file mounted `:ro`, not an `environment:` entry — an env var leaks into `docker inspect`, and a writable file lets the container rewrite its own credential. `secrets/` is gitignored and `make check` asserts nothing under it is tracked.
- Values in `.env` that contain `$` (bcrypt hashes) must be **single-quoted**: `.env` is `.`-sourced by shell under `set -u`, where `$2a$10$…` aborts with `$2: unbound variable` and silently skips everything after it. Prefer reading `.env` with `sed` over sourcing it.
- Do not output the real username or absolute home directory; refer to env vars instead.

Python Scripts (`scripts/`)

- Small, focused functions (one responsibility); avoid boolean flag parameters—split functions instead.
- Keep side effects (filesystem, network) thin and centralized in `main()`; core logic should be pure and testable.
- Favor meaningful names and constants over magic numbers; prefer dataclass / simple object to long param lists.
- Catch narrow exceptions; only broad catch at top-level for clean exit code and context-rich error message.

Shell Snippets

- Start with `#!/usr/bin/env bash` + `set -euo pipefail` + `IFS=$'\n\t'` when creating a new script. Drop the `-e` only if the script has a rollback or reporting path it must reach, and say so in a comment.
- Never put a `#` comment inside a backslash-continued command — it swallows the rest of the command.
- Quote variable expansions and prefer arrays for argument lists; avoid `eval`.

General Guidance

- Readability and maintainability first—optimize only after measurement.
- Avoid introducing heavy new dependencies for trivial tasks; propose before adding.
- Provide actionable error messages (include key identifiers like path, service name, counts).
- Update or add a healthcheck when adding a service or changing its main port. No two services may publish the same host port; internal WebUIs bind `127.0.0.1`.
- A `2xx`, a green Test button and a "sent" log line are not evidence in this repo. Prove a change by a second, independent observation — and note that `docker compose up -d` does not apply an edit to a bind-mounted file's **contents** (compose compares service config), so a credential or conf change needs `restart`.
- If unsure about a structural change, propose a short plan before editing.

Out of Scope

- Do not change overall response tone/style or impose arbitrary length limits.
- Do not reference external private resources.

See `copilot-instructions.extensive.md` in the same directory for the comprehensive project guide.
