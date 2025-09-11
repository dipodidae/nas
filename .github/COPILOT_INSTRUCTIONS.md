# Copilot Project Instructions

Authoritative guidance for GitHub Copilot / AI assistants working in this repository. Read fully before generating code or docs.

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
