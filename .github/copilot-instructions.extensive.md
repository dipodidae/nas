# Copilot Project Instructions

Authoritative (long-form) guidance for GitHub Copilot / AI assistants working in this repository.

Short-form instruction file (auto-injected into chat per GitHub feature preview): see `./copilot-instructions.md` for the concise bullet list. Keep that file to terse, self-contained statements only; place rationale, extended examples, and broader patterns here.

---

## 1. Project Overview

Infrastructure-as-code + minimal landing page for a self‑hosted NAS / media stack (Raspberry Pi 5 tuned, but generic Linux capable). Core services: Jellyfin, \*arr suite (Sonarr/Radarr/Bazarr/Prowlarr), qBittorrent, Nextcloud, SWAG (reverse proxy + TLS), Watchtower, Autoheal, Cloudflare DDNS, supporting utilities. Frontend: a tiny static root page built with Vite + TailwindCSS.

Focus areas:

- Deterministic Docker Compose definitions (single file)
- Operational safety (resource limits, healthchecks, restarts)
- Idempotent config layout via environment variables
- Low-friction onboarding & extension

If unsure: prefer explicitness over cleverness.

---

## 2. Tech & Conventions

| Domain          | Convention                                                                                                                                          |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| Orchestration   | `docker-compose.yml` single source; keep service keys alphabetized inside each service block where practical (volumes/env/ports logically grouped). |
| Images          | Prefer `lscr.io/linuxserver/*` where already used. Pin with `:latest` unless security/compat requires digest (offer rationale if changing).         |
| Networking      | Single custom bridge network `nas-network`. New services join it unless host mode is strictly required (explain).                                   |
| Healthchecks    | Always add a `HEALTHCHECK` using lightweight `curl -f` (or process probe). Interval 30–60s, retries 3, timeout ≤10s.                                |
| Logging         | Use `json-file` w/ size rotation if logs may grow. Keep consistent w/ existing pattern.                                                             |
| Volumes         | Use `${CONFIG_DIRECTORY}` for app configs; `${SHARE_DIRECTORY}` for media/data. Never hard‑code user home paths.                                    |
| User Mapping    | Always include `PUID`, `PGID`, `TZ` where supported.                                                                                                |
| Restart Policy  | `unless-stopped` (or `always` when justified e.g. watchdog services).                                                                               |
| Labels          | `com.centurylinklabs.watchtower.enable=true` only for containers meant to auto‑update. Add `swag=enable` to expose via reverse proxy (if HTTP UI).  |
| Resource Limits | Provide conservative `deploy.resources.limits` & `reservations` where useful. Avoid starving Jellyfin transcoding.                                  |

---

## 3. Adding a New Service (Template)

Minimal skeleton (adjust ports/paths):

```yaml
myservice:
  image: vendor/myservice:latest
  container_name: myservice
  environment:
    - PUID=${PUID}
    - PGID=${PGID}
    - TZ=${TZ}
  volumes:
    - ${CONFIG_DIRECTORY}/myservice:/config
  ports:
    - 1234:1234
  networks:
    - nas-network
  restart: unless-stopped
  labels:
    - com.centurylinklabs.watchtower.enable=true
    - swag=enable # remove if not externally proxied
  healthcheck:
    test: [CMD, curl, -f, 'http://localhost:1234/']
    interval: 45s
    timeout: 5s
    retries: 3
    start_period: 30s
```

Guidelines:

- Use `depends_on` only for hard runtime ordering (DB before app). Avoid chaining everything.
- Justify any `cap_add`, `privileged`, `network_mode: host` in a comment.
- If hardware acceleration: comment blocks with enabling instructions (mirroring Jellyfin style).

---

## 4. Environment Variables

Primary vars (see README): `PUID`, `PGID`, `TZ`, `CONFIG_DIRECTORY`, `SHARE_DIRECTORY`, `DOMAIN`, `ADMIN_EMAIL`, `CLOUDFLARE_API_TOKEN`, `JELLYFIN_PUBLISHED_URL`, `QBITTORRENT_USER`, `QBITTORRENT_PASS`, `WATCHTOWER_SCHEDULE`.

Rules:

- Never bake secrets into committed files.
- If introducing a new required var: update README (+ example snippet) and reference it in new service section.
- Provide safe defaults only when non-sensitive.

---

## 5. Security & Hardening

- Prefer dropping privileges (`no-new-privileges:true`, avoid `privileged`).
- Only map the Docker socket through the existing `dockerproxy` (read-only). Never duplicate raw socket mounts.
- Justify any additional capabilities with a comment.
- Keep public endpoints TLS-terminated by SWAG; internal services should not self‑manage certs unless necessary.

---

## 6. Reverse Proxy Exposure

Add label `swag=enable` for SWAG auto-proxy. Do not expose random high ports publicly if proxying. For new subdomains ensure DNS wildcard covers it or instruct user to add record.

---

## 7. Health & Self-Healing

- Every user-facing service should define a healthcheck.
- Use the lightest stable endpoint (avoid heavy database pages).
- Autoheal relies on health state; keep intervals reasonable (avoid flapping).

---

## 8. Updates Strategy

- Watchtower updates only labeled services. If pinning to a digest or specific tag, explain why in a comment.
- Avoid major version bumps without migration note in README or release notes.

---

## 9. Rootpage (Frontend) Guidelines

- Stack: Vite + vanilla JS + TailwindCSS.
- Keep bundle minimal; do not introduce frameworks (React/Vue) without explicit request.
- Use utility classes; custom CSS belongs in `rootpage/src/style.css` (group related animation blocks, annotate major sections).
- All assets go under `rootpage/public/`.
- Avoid large images; prefer SVG or CSS effects.
- If adding JS modules, keep side-effect scripts in `src/` and import from `main.js`.

### Accessibility / Performance

- Ensure animations respect `prefers-reduced-motion` (extend existing pattern).
- Avoid blocking main thread with long loops; use `requestAnimationFrame` where needed.

---

## 10. Coding Style & Linting

- Follow existing ESLint config (`@antfu/eslint-config`). Run `pnpm lint` (or `npm run lint` if user chooses) before commits touching JS.
- Prefer clarity over micro-optimization.
- Keep YAML indentation at 2 spaces.

### Shell / Bash Style (Brief)

Use Bash only for small orchestration wrappers; migrate to Python if a script exceeds ~100 lines or contains complex logic.

Core rules:

- Shebang: `#!/usr/bin/env bash` then immediately: `set -euo pipefail` and `IFS=$'\n\t'`.
- 2-space indent; no tabs. Guard clauses > deeply nested blocks.
- Quote expansions by default: `"${var}"`; forward args with `"$@"`.
- Use `$(command)` not backticks; prefer `[[ ... ]]` for tests; numeric tests with `(( expr ))`.
- Arrays for multi-arg lists: `FLAGS=(--opt-a --opt-b=value); tool "${FLAGS[@]}"`.
- Avoid: `eval`, aliases, `expr`, `$[ ]`, `let`, unquoted globs, `cmd | while read` subshell pitfalls.
- Replace `cmd | while read` with process substitution: `while read -r l; do ...; done < <(cmd)` or `readarray -t lines < <(cmd)`.
- Pipeline formatting (multi-line):
  ```bash
  cmd1 \
    | cmd2 \
    | cmd3
  ```
- Error helper: `err() { echo "[$(date +'%Y-%m-%dT%H:%M:%S%z')] $*" >&2; }`.
- Return early on failure: `if ! mv -- "${src}" "${dst}"; then err "move failed"; exit 1; fi`.
- Use `./*` instead of bare `*` when removing files to avoid `-file` flag hazards.
- Functions: `snake_case`; constants/env: `UPPER_SNAKE`; keep functions grouped near top; final `main "$@"` when non-trivial.
- Run ShellCheck on new scripts.

Minimal skeleton:

```bash
#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'
err() { echo "[ERR] $*" >&2; }
main() { :; }
main "$@"
```

### Python Clean Code Quick Reference (Inspiration: clean-code-python)

Keep this brief, pragmatic subset aligned with existing `scripts/` patterns:

**Variables**

- Meaningful, pronounceable, searchable; avoid cryptic abbreviations (`current_date`, not `ymdstr`).
- Consistent vocabulary for same concept (use `indexer_*` everywhere, not `provider_*` sometimes).
- Promote important magic numbers to UPPER_SNAKE constants (`SECONDS_IN_DAY`).
- Avoid redundant context (`car.make` not `car.car_make`).

**Functions**

- Do one thing; if you feel compelled to add a boolean flag, split into two functions.
- Prefer ≤2 required positional params; bundle related params into a dataclass / typed object if growing.
- Names should state intent (`prune_archives`, not `do_prune`).
- Single abstraction level inside a function; extract tokenization/parsing loops to helpers.
- Avoid side effects except at boundaries (I/O, environment) and centralize them in `main()`.

**Arguments & Defaults**

- Use default parameter values instead of `if arg is None: arg = ...` where appropriate.
- Avoid mutable defaults; use `None` + create inside body if needed.

**Classes / SOLID (Only When Needed)**

- SRP: Each class encapsulates one reason to change (e.g. `ProwlarrApiClient`).
- OCP: Prefer adding a new small class or overriding a focused method instead of modifying broad internals.
- LSP: Subclasses must not narrow method signatures or alter expected return types.
- ISP: Keep abstract / mixin surfaces minimal; compose multiple tiny ABCs or mixins instead of one fat base.
- DIP: Depend on small abstractions (protocol / simple function contract) rather than concrete heavy objects.

**DRY & Abstraction**

- Factor duplicated logic early if semantics are identical; otherwise wait until patterns stabilize.
- Prefer a well-named helper function over premature class hierarchies.

**Naming Patterns**

- Functions & variables: `snake_case`; Classes: `CamelCase`; Constants: `UPPER_SNAKE`.
- Boolean predicates start with `is_`, `has_`, `needs_` where clarity improves call-sites.

**Control Flow & Clarity**

- Return early on invalid state (guard clauses) to avoid nested pyramids.
- Replace complex branching with dictionary dispatch / strategy objects only when it simplifies reading.

**Error Handling**

- Catch the narrowest exception feasible; broad `except Exception` only at top-level orchestration.
- Provide actionable error messages; prefer including context (`path`, `service`, `size_mb`).

**Side Effects & Purity**

- Pure functions accept inputs, return outputs, no global mutation—favored for core logic.
- Side-effect functions (I/O, network, filesystem) should be thin wrappers around pure core.

**Performance**

- Optimize only after measurement; readability first. Use streaming (iterators, chunked reads) for large files.

**Testing Hooks**

- Design helpers to be importable without executing code (no work at import time other than constants & light checks).
- Expose core logic via functions returning data (status code, structured result) for easy test assertions.

**Anti-Patterns To Avoid**

- Boolean parameter switches, sprawling 200+ line functions, deep nested try/except blocks, wide dataclasses acting as unstructured bags, overuse of inheritance where composition or a simple function suffices.

Use this as a heuristic checklist—do not over-engineer tiny maintenance scripts.

---

#### Clean Code (clean-code-python) Derived Ultra-Brief Checklist

Use this distilled list while editing Python in `scripts/`.

Variables:

- Meaningful & pronounceable; consistent domain vocabulary (same concept => same root name).
- Searchable & explanatory: replace magic numbers/strings with upper-snake constants and named regex groups.
- Avoid mental mapping (`location` not `item`), redundant context (`car.make` not `car.car_make`).
- Prefer default parameters over `if x is None: x = ...` when semantically identical.

Functions:

- Single responsibility & single abstraction level; extract loops/parsing/IO.
- ≤2-3 required params; otherwise bundle into a dataclass / TypedDict / simple object.
- No boolean flags to branch behavior—split functions.
- Names express action/result (`get_active_clients`, `prune_logs`).
- Centralize side effects; keep core pure.

Classes / SOLID:

- SRP: one reason to change.
- OCP: add behavior by extension (override narrow hook) not editing broad internals.
- LSP: subclasses keep signatures & contracts compatible.
- ISP: many tiny ABCs / mixins over one bloated base.
- DIP: depend on slim protocols (duck-typed surface) not concrete heavy classes.

Side Effects:

- Isolate filesystem/network/env mutations; pass data in/out rather than mutating globals.

DRY:

- Extract identical logic early; defer abstraction if similarities are still evolving.

Error Handling:

- Catch narrow exceptions; actionable messages with context values.
- Only broad catch at top orchestration layer returning clean exit codes.

Performance:

- Readability first; measure before optimizing. Stream large inputs (iterators, chunking) and avoid loading huge files fully when unnecessary.

Testing & Import Hygiene:

- Module import should not perform heavy work. Guard executable code under `if __name__ == "__main__": main()`.

Anti-Patterns (rename / refactor on sight):

- Flag params, duplicate 30+ line near-identical blocks, deep nesting >3, silent broad excepts, mega utility classes acting as unstructured bags, global mutable state.

Rule of Thumb: If explaining a function requires “and then it also…”, it probably does too much.

---

## 11. Commit Messages

Conventional-ish style (not enforced):

```
feat: add <service/component>
fix: resolve <issue>
chore: update deps / infra
docs: improve README or instructions
refactor: internal restructure no behavior change
perf: performance tweak
ci: automation / workflow changes
```

Add a short scope if useful (e.g. `feat(rootpage): ...`).

---

## 12. AI Assistant Guardrails

Do:

- Verify referenced file paths exist.
- Provide full added file contents (no ellipses) when creating new files.
- Explain non-obvious infra changes.

Don't:

- Invent services or environment variables.
- Remove existing security-hardening lines silently.
- Introduce heavyweight dependencies for trivial tasks.

If uncertain about an irreversible change: propose first.

---

## 13. Testing & Validation Steps (Manual)

Before suggesting merge:

1. `docker compose config` (validate syntax) – ensure no warnings.
2. For changed services: note restart impact & persistence implications.
3. Rootpage: run `pnpm build` and verify output under `rootpage/dist`.
4. Confirm new healthcheck command returns 0 locally (simulate with `curl`).

---

## 14. Performance Notes

- Jellyfin: leave CPU unconstrained for bursts (only reservations). Avoid adding strict limit unless user requests.
- Keep cache/temp on faster storage (`/tmp` tmpfs already used).
- Avoid over-constraining I/O heavy apps simultaneously.

---

## 15. Extensibility Patterns

When adding a dependent service (e.g., database):

- Define the database container first.
- Use internal service name as host.
- Add a readiness healthcheck for the database if needed.
- Document backup/restore steps in README if stateful.

---

## 16. Documentation Expectations

Any new service or env var requires README update (short subsection with purpose, ports, and key config vars). Keep formatting consistent (tables, bullet lists).

---

## 17. Common Mistakes to Avoid

| Mistake                                   | Why Bad                              | Correct Approach                       |
| ----------------------------------------- | ------------------------------------ | -------------------------------------- |
| Mapping entire host `/` as volume         | Security & accidental overwrite risk | Map only required directories          |
| Using `latest` plus digest simultaneously | Redundant / misleading               | Use one: tag OR digest                 |
| Missing healthcheck on UI service         | Breaks autoheal & observability      | Add lightweight curl endpoint          |
| Copying env vars into compose inline      | Duplication & drift                  | Reference `${VAR}` from `.env`         |
| Adding multiple networks without reason   | Complexity                           | Stay on `nas-network` unless justified |

---

## 18. Requesting Large Changes

For multi-service refactors, stage changes logically (e.g., add DB, then update dependent services). Provide migration notes.

---

## 19. License & Attribution

Respect upstream image licenses. Keep third-party snippets minimal and attributed if non-trivial.

---

## 20. When In Doubt

Prefer proposing an outline before invasive edits. Clarity > speed.

---

Happy hacking. Keep it clean, observable, and reproducible.
