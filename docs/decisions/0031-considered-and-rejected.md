# ADR-0031 — Considered and rejected

**Date:** 2026-09-02
**Status:** accepted

One record for the things that keep getting re-proposed, so the answer is
lookup-able rather than re-derived. Each entry says _why for this repo_, because
several of these are perfectly good tools that this particular stack is the
wrong home for.

Huntarr has its own record (ADR-0029) because it is a security rejection with
specifics, and because a work order actively recommended it.

## Compose UIs: Komodo, Dockge, Portainer — no

This is a conclusion, not a hedge. The `Makefile` + `check-invariants.sh` +
pre-commit hook is a **better control plane than any of them for this repo**,
and each fails on something structural rather than stylistic:

- **Komodo** substitutes variables with its own bracket syntax instead of
  standard Compose interpolation. This project is `${VAR}`-heavy by design —
  every path, the domain, every credential — and `include:`-wired across nine
  files. A different interpolation dialect does not "work with" that; it
  reinterprets it.
- **Dockge** assumes one stack per directory. This is deliberately **one project
  across many files** (ADR-0000), because cross-module `depends_on` only works
  inside a single project. That assumption is the thing the layout exists to
  avoid.
- **Portainer**'s editor takes ownership of the YAML. The comments in these
  compose files _are_ the incident history — every `INVARIANT:` line with its
  ADR pointer. A tool that round-trips the YAML through its own serialiser
  destroys the highest-value content in the repo.

The deeper reason: a UI's value is convenience, and what this stack lacked was
never convenience. It was **assertions**. `make check` fails a commit; a web
form cannot.

## A second notification channel — no

Everything goes to the self-hosted ntfy (ADR-0012), and the two-topic split
(`nas-alerts` = act on it, `nas-media` = nice to know) is load-bearing: routing
informational events at `nas-alerts` buries the failures the alerting exists to
surface.

Applied twice in this pass: Scrutiny's own notifier stack was not used, and
Beszel's `emails` list was left deliberately **empty** — email would also have
needed SMTP this box does not have, making it a silently dead second channel,
which is worse than none.

## A second queue cleaner, updater, or armed deleter — no

One of each, and each one's boundaries are written down.

- **Decluttarr** overlaps Cleanuparr almost entirely. Two queue cleaners racing
  on the same queue is strictly worse than one, and Cleanuparr's rules here are
  tuned against real incidents (ADR-0017).
- **A second update tool** is moot: nothing applies updates at all any more
  (ADR-0025), and `diun` covers every image including the pinned ones
  (ADR-0024).
- **A second armed deleter** was actively declined in this pass: qui's orphan
  scanner stays off, and the measurement is the argument — 94 % of the orphans
  it would find are slskd's data, which it cannot know about (ADR-0027).

## Anything requiring a Docker socket mount — no

ADR-0013, no exceptions. Tested in this pass rather than assumed: Beszel's agent
reaches Docker over `tcp://dockerproxy:2375`, and the endpoint set narrowed in
ADR-0025 already covered it without re-widening.

The corollary bites harder than it looks: **a tool that needs host networking
effectively needs the socket published**, because a host-networked container
cannot resolve `dockerproxy` by service DNS. That is why Beszel's agent does not
use host networking despite its own docs requiring it for NIC stats (ADR-0028),
and `make check` now asserts nothing does.

## VPN / gluetun — no

ADR-0019. Out of scope by instruction, and the decision is the owner's.

## Media backup — out of scope

4.6 TB under `${SHARE_DIRECTORY}` is not backed up, by choice. The off-box
`restic` job covers **config only** and is still awaiting a destination. Nothing
in this pass changed either.

## Dozzle — proposed, not adopted

Cross-container log search in one pane is genuinely useful, and it clears the
socket bar (it supports a socket-proxy connection). It was left out for an
honest reason rather than a rule: the `10m`/`2` json-file log budget means Docker
keeps only minutes of history for a chatty service, and **Dozzle can only search
what Docker kept**. It would look like log retention without being it.

Wanting real retention is a separate decision, and probably a "no" on a single
disk with no redundancy.

## Gatus — proposed, not adopted, and additive if ever

Declarative endpoint assertions in version control — TLS expiry, response-body
conditions, latency budgets — would suit this repo's culture exactly: single Go
binary, YAML-only config, reviewable in a diff.

It is **additive to `scripts/stack_watchdog.py`, never a replacement.** The
watchdog detects _a service defined in compose with no container at all_, which
is the actual historical failure mode (13 h of qBittorrent, 7 days, a month of
absent autoheal) and which no endpoint prober can see: there is no endpoint to
probe.

## Forward auth over the whole public surface — proposed, not adopted

16 subdomains with per-app auth, plus one hand-rolled basic-auth door on
`ongehoord`. SWAG ships `tinyauth-location.conf.sample` and Authelia samples and
nothing here uses them; Tinyauth or Pocket ID would give one door for all of it.

Not in this pass, deliberately: under ADR-0022 the **conf is the mechanism**, so
adding forward auth means editing every tracked proxy-conf at once — the exact
opposite of a change that belongs in a batch with eleven others. Known sharp
edge for whoever does it: Tinyauth v5.1.0 made a `login_for` parameter mandatory
for redirects, which broke SWAG's older manual-redirect confs. Pin a version,
test the redirect path, re-run `make check`.

## Kometa-style collections for Jellyfin — optional polish, not infrastructure

Only worth it if curated collections are actually wanted. The Jellyfin-native
options are young and low-star. Calling it infrastructure would overstate it.
