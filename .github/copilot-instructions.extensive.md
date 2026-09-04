# Copilot Project Instructions

Authoritative (long-form) guidance for GitHub Copilot / AI assistants working in this repository.

Short-form instruction file (auto-injected into chat per GitHub feature preview): see `./copilot-instructions.md` for the concise bullet list. Keep that file to terse, self-contained statements only; place rationale, extended examples, and broader patterns here.

---

## 1–9. Project facts: read the authoritative docs, not this file

**Sections 1–9 of this file were deleted on 2026-09-04 because they had become
actively wrong, and a stale second copy of the rules is worse than no copy.**
They described a Raspberry Pi 5 host, a single Compose file, Watchtower,
Cloudflare DDNS, `QBITTORRENT_USER`/`QBITTORRENT_PASS` on the container, and a
service template whose labels included
`com.centurylinklabs.watchtower.enable=true`. Every one of those is now false,
and the last is a label `make check` **rejects**.

This repo has three authoritative sources and they are kept current:

| Question                                                                    | Read                                                  |
| --------------------------------------------------------------------------- | ----------------------------------------------------- |
| Conventions: Python, shell, compose rules, the env-var contract, exit codes | `AGENTS.md` — binding                                 |
| Orientation, commands, the gotchas that have cost downtime                  | `CLAUDE.md`                                           |
| Why a line exists and what broke without it                                 | `docs/decisions/` (35 ADRs, start at its `README.md`) |
| Services, ports, URL map, the door per route, known gaps                    | `README.md`                                           |

Task-shaped guidance lives in `.claude/skills/`: `nas-public-surface` (routes and
the tinyauth door), `nas-runtime-vs-repo` (which gate an assertion belongs in),
`verifying-by-effect` (what counts as proof here), `hunting-silent-failure`,
`nas-cron-jobs`, `nas-music-pipeline`.

The five things most likely to be got wrong by anyone working from memory:

1. **One Compose project, many files** (`compose.yaml` + `compose/*.yaml` +
   `webapps/*/compose.yaml`, wired with `include:`). Shared shape comes from
   `extends` on `compose/_fragments.yaml`, not from copy-paste. ADR-0000.
2. **Nothing auto-updates.** Watchtower is retired; `diun` notifies and a human
   applies. Do not add a `com.centurylinklabs.watchtower.*` label. ADR-0025.
3. **The proxy-conf publishes a subdomain, not the `swag=enable` label**, and a
   conf edit is applied with `make swag-apply` — Docker binds each conf by
   inode, so a `git checkout` detaches the mount and nginx serves the old file.
   ADR-0022, ADR-0034.
4. **Thirteen routes sit behind one tinyauth door**; `jellyfin`, `nextcloud` and
   `ntfy` are excluded because their clients cannot follow a `302`. Never add
   `auth_basic` beside the forward-auth include — it preempts it. ADR-0034.
5. **A credential a container reads is a `0600` file mounted `:ro`**, never an
   `environment:` entry. ADR-0011, ADR-0033.

Run `make check && make lint` after any compose change, and
`make verify-runtime` when the claim is about the running system.

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

- **Pi-era resource limits were removed for the MS01 host** (`mem_limit`, `cpus`,
  `blkio_config`, `ulimit`). Do not reintroduce them without a reason and an ADR —
  ADR-0001. Where a `mem_limit` does exist, `memswap_limit` must equal it or it
  balloons into host swap (ADR-0007).
- Keep cache/temp on faster storage (`/tmp` tmpfs already used).
- Measure before constraining. `make measure-qbittorrent-stop` is the pattern:
  a number in the commit message, not an adjective.

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

| Mistake                                                             | Why Bad                                                                     | Correct Approach                          |
| ------------------------------------------------------------------- | --------------------------------------------------------------------------- | ----------------------------------------- |
| Mapping entire host `/` as volume                                   | Security & accidental overwrite risk                                        | Map only required directories             |
| Using `latest` plus digest simultaneously                           | Redundant / misleading                                                      | Use one: tag OR digest                    |
| Missing healthcheck on UI service                                   | Breaks autoheal & observability                                             | Add lightweight curl endpoint             |
| Copying env vars into compose inline                                | Duplication & drift                                                         | Reference `${VAR}` from `.env`            |
| Adding multiple networks without reason                             | Complexity                                                                  | Stay on `nas-network` unless justified    |
| Adding a `com.centurylinklabs.watchtower.*` label                   | Watchtower is retired; `make check` rejects it                              | `diun` notifies, a human applies          |
| `nginx -s reload` after editing a proxy-conf                        | Docker binds the conf by inode; a replaced file never reaches the container | `make swag-apply`                         |
| `docker compose up -d` after editing a bind-mounted file's contents | Compose compares service config, not file contents — it is a no-op          | `docker compose restart <svc>`            |
| `auth_basic` next to a forward-auth include                         | It preempts rather than stacks; every valid session is locked out           | Pick one; here it is the tinyauth include |
| A credential in an `environment:` block                             | Leaks into `docker inspect`                                                 | A `0600` file mounted `:ro`               |
| An unquoted `$` value in `.env`                                     | `.`-sourced under `set -u` it aborts the shell and skips every later check  | Single-quote it                           |

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
