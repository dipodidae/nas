# NAS stack -- operational targets.
#
# These encode the *deliberate* update workflow the decision records describe.
# The important ones are the things you must NOT do on a schedule:
# `update-qbittorrent` and `pull-jellyfin` exist because Watchtower's
# stop->remove->create is not atomic and has twice left no container at all
# (ADR-0006).
#
# `pnpm run <script>` still works for everything in package.json; this file
# adds what compose alone cannot express.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# Read PUID/PGID/CONFIG_DIRECTORY out of .env without exporting the whole file.
ENV_FILE ?= .env
getenv = $(shell sed -n 's/^$(1)=//p' $(ENV_FILE) 2>/dev/null | tail -1)
PUID  ?= $(or $(call getenv,PUID),1000)
PGID  ?= $(or $(call getenv,PGID),1000)
CONFIG_DIRECTORY ?= $(call getenv,CONFIG_DIRECTORY)

.PHONY: help check lint config diun-manifest bootstrap up down logs pull \
        pull-jellyfin update-qbittorrent measure-qbittorrent-stop \
        submodules install-hooks verify-runtime backup-offsite

help: ## Show this help
	@echo "NAS stack targets:"
	@grep -hE '^[a-z][a-z0-9_-]*:.*##' $(MAKEFILE_LIST) \
	  | sed 's/:.*##/\t/' \
	  | awk -F'\t' '{printf "  \033[36m%-28s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- validation

check: ## Assert every invariant the incidents taught us (scripts/check-invariants.sh)
	@scripts/check-invariants.sh

lint: ## Validate the compose model renders (matches CI)
	@docker compose config -q && echo "compose model OK"

config: ## Print the fully-resolved, merged compose model
	@docker compose config

diun-manifest: ## Regenerate diun/manifest.yml from the compose model (ADR-0024)
	@.venv/bin/python scripts/emit_diun_manifest.py 2>/dev/null \
	  || python3 scripts/emit_diun_manifest.py
	@echo 'Commit the result -- make check asserts it matches the compose model.'

# ---------------------------------------------------------------- lifecycle

bootstrap: ## One-time host prep: create the network and pre-chown non-LSIO config dirs
	@echo "==> nas-network"
	@docker network inspect nas-network >/dev/null 2>&1 \
	  && echo "    exists" \
	  || docker network create \
	       --driver bridge \
	       --subnet 172.30.0.0/24 --gateway 172.30.0.1 \
	       --opt com.docker.network.bridge.enable_ip_masquerade=true \
	       nas-network
	@# `docker compose up` creates the network itself; this target exists so the
	@# network can be created ahead of time, and so the chowns below have a home.
	@if [ -z "$(CONFIG_DIRECTORY)" ]; then \
	  echo "!!! CONFIG_DIRECTORY is not set in $(ENV_FILE); cannot pre-chown." >&2; exit 2; \
	fi
	@# qui, ntfy and diun are NOT linuxserver images: no root init chowns their
	@# if Docker auto-creates these as root on first `up` they crash-loop on
	@# `permission denied`. ADR-0014.
	@# Deliberately NOT scrutiny: it is the inverse case. It runs as root (it
	@# must, to hold CAP_SYS_ADMIN for the NVMe ioctl) and has no DAC_OVERRIDE,
	@# so a ${PUID}-owned config dir makes root fall through to the "other"
	@# permission bits and fail with `Permission denied` on influxdb's config.
	@# Docker's default -- auto-creating the bind-mount source as root:root --
	@# is correct there. Do not add it to this loop. ADR-0023.
	@for d in qui ntfy diun beszel beszel-agent; do \
	  p="$(CONFIG_DIRECTORY)/$$d"; \
	  echo "==> $$p -> $(PUID):$(PGID)"; \
	  mkdir -p "$$p"; \
	  if [ "$$(stat -c '%u:%g' "$$p")" != "$(PUID):$(PGID)" ]; then \
	    chown "$(PUID):$(PGID)" "$$p" || sudo chown "$(PUID):$(PGID)" "$$p"; \
	  else echo "    already correct"; fi; \
	done
	@echo "bootstrap complete."

up: ## Start the whole stack (see the ongehoord caveat in its compose file)
	@docker compose up -d

down: ## Stop and remove the whole stack
	@docker compose down

logs: ## Follow logs for all services
	@docker compose logs -f

pull: ## Pull newer images for every service that is not locally built
	@docker compose pull --ignore-buildable

# ------------------------------------------------- deliberate single-service
# updates. Both of these are Watchtower-opt-out on purpose (ADR-0006).

pull-jellyfin: ## Deliberately update Jellyfin, then wait for healthy
	@docker compose pull jellyfin
	@docker compose up -d jellyfin
	@$(MAKE) --no-print-directory wait-healthy SVC=jellyfin

update-qbittorrent: ## Deliberately update qBittorrent (reminds you to bump the pinned tag first)
	@current=$$(sed -n 's|.*image: lscr.io/linuxserver/qbittorrent:||p' compose/media-download.yaml); \
	echo "qbittorrent is pinned to: $$current"; \
	echo; \
	echo "The tag is pinned ON PURPOSE (floor >= 5.2.2, upstream #24357 / fix"; \
	echo "#24363) and Watchtower is deliberately not allowed near it -- its"; \
	echo "non-atomic recreate left no container at all for 13h on 2026-09-01."; \
	echo "See docs/decisions/0005-qbittorrent-pinned-tag.md and 0006-*.md"; \
	echo; \
	echo "1. Check what is current:"; \
	echo "     https://github.com/linuxserver/docker-qbittorrent/pkgs/container/qbittorrent"; \
	echo "2. Edit the image: tag in compose/media-download.yaml"; \
	echo "3. Re-run this target."; \
	echo; \
	read -r -p "Has the tag already been bumped to the version you want? [y/N] " a; \
	case "$$a" in [yY]*) ;; *) echo "Nothing done."; exit 0 ;; esac; \
	docker compose pull qbittorrent; \
	docker compose up -d qbittorrent
	@$(MAKE) --no-print-directory wait-healthy SVC=qbittorrent

.PHONY: wait-healthy
wait-healthy:
	@echo "==> waiting for $(SVC) to report healthy (do not walk away)"
	@for i in $$(seq 1 60); do \
	  s=$$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' $(SVC) 2>/dev/null || echo missing); \
	  printf '\r    t=%3ds  %s          ' $$((i*5)) "$$s"; \
	  case "$$s" in \
	    healthy|no-healthcheck) echo; echo "    $(SVC): $$s"; exit 0 ;; \
	    missing) echo; echo "!!! $(SVC) has NO CONTAINER -- this is the ADR-0006 failure mode." >&2; exit 1 ;; \
	  esac; \
	  sleep 5; \
	done; \
	echo; echo "!!! $(SVC) did not become healthy in 300s." >&2; \
	docker compose logs --tail 40 $(SVC); exit 1

# ---------------------------------------------------------------- measurement

measure-qbittorrent-stop: ## Time a graceful qBittorrent stop (re-measure as torrent count grows)
	@echo "Baseline 2026-09-01 at 128 torrents: 6.2s WITH CAP_KILL, 120.3s without."
	@echo "stop_grace_period is 120s -- raise it only if the measured time"
	@echo "approaches it. ADR-0004."
	@echo
	@n=$$(docker exec qbittorrent sh -c 'ls /config/qBittorrent/BT_backup/*.torrent 2>/dev/null | wc -l' || echo '?'); \
	  echo "torrents currently in BT_backup: $$n"
	@echo
	@echo "==> autoheal will try to restart this within ~30s of it going away."
	@echo "    That is expected (ADR-0010); the timing below is still valid."
	@echo
	@start=$$(date +%s.%N); \
	  docker compose stop qbittorrent; \
	  end=$$(date +%s.%N); \
	  echo; printf 'graceful stop took: %.1fs\n' "$$(echo "$$end - $$start" | bc)"
	@echo
	@echo "==> checking for a clean shutdown in the log"
	@docker logs --tail 30 qbittorrent 2>&1 | grep -i "saving resume data\|shutdown" || \
	  echo "    !!! no 'Saving resume data completed' -- this is the CAP_KILL symptom (ADR-0004)"
	@echo
	@echo "==> bringing it back"
	@docker compose up -d qbittorrent
	@$(MAKE) --no-print-directory wait-healthy SVC=qbittorrent

# ---------------------------------------------------------------- submodules

submodules: ## Update the ongehoord + playlist-generator submodules to their tracked branches
	@git submodule update --remote --merge webapps/ongehoord/src webapps/jellyfin-playlist-generator
	@git submodule status webapps/ongehoord/src webapps/jellyfin-playlist-generator
	@echo
	@echo "Both are locally-built images, so a pull does nothing -- rebuild:"
	@echo "  docker compose up -d --build playlist-generator"
	@echo "  webapps/ongehoord/redeploy.sh   # ongehoord needs buildx --network=host"

install-hooks: ## Install the pre-commit hook that runs `make check`
	@hook="$$(git rev-parse --git-path hooks)/pre-commit"; \
	printf '%s\n' \
	  '#!/usr/bin/env bash' \
	  '# Installed by `make install-hooks`. Asserts the compose invariants.' \
	  '# Skip once with: git commit --no-verify' \
	  'set -euo pipefail' \
	  'root="$$(git rev-parse --show-toplevel)"' \
	  'cd "$$root"' \
	  '# Only run when something that shapes the compose model changed.' \
	  'if git diff --cached --name-only | grep -qE "^(compose\.ya?ml|compose/|webapps/[^/]+/compose\.ya?ml|\.env)"; then' \
	  '  echo "pre-commit: checking compose invariants..."' \
	  '  scripts/check-invariants.sh' \
	  'fi' \
	  > "$$hook"; \
	chmod +x "$$hook"; \
	echo "installed $$hook"

verify-runtime: ## Assert the RUNNING containers match the invariants (not just the config)
	@rc=0; \
	echo "==> every compose service has a container (ADR-0006)"; \
	for s in $$(docker compose config --services); do \
	  docker inspect "$$s" >/dev/null 2>&1 || { echo "    !!! $$s: NO CONTAINER"; rc=1; }; \
	done; [ $$rc -eq 0 ] && echo "    all present"; \
	echo "==> no stray compose.override.yaml"; \
	if [ -e compose.override.yaml ] || [ -e compose.override.yml ]; then \
	  echo "    !!! compose.override.yaml present. It is gitignored AND auto-loaded,"; \
	  echo "        so git status will not show it and every compose command is affected."; \
	  rc=1; \
	else echo "    none"; fi; \
	echo "==> qbittorrent holds CAP_KILL at runtime (ADR-0004)"; \
	docker exec qbittorrent sh -c 'grep -E "^Cap(Prm|Eff|Bnd)" /proc/1/status' \
	  | gawk '{ v=strtonum("0x" $$2); if (!and(v, 32)) { printf "    !!! %s lacks KILL\n", $$1; bad=1 } } \
	          END { if (bad) { print "        every stop becomes a 120s SIGKILL"; exit 1 } \
	                print "    ok: KILL in Prm/Eff/Bnd" }' || rc=1; \
	echo "==> swag nginx can signal its own workers (ADR-0021)"; \
	docker exec swag sh -c 'for p in /proc/[0-9]*; do \
	    case "$$(tr -d "\0" < $$p/cmdline 2>/dev/null)" in "nginx: worker process") \
	      kill -0 $$(basename $$p) 2>/dev/null && echo "    ok: worker signalable" \
	        || { echo "    !!! EPERM signalling nginx worker -- reload and graceful stop are broken"; exit 1; }; \
	      break;; esac; done' || rc=1; \
	echo "==> nothing but dockerproxy has the Docker socket (ADR-0013)"; \
	bad=$$(docker ps -q | xargs -r docker inspect \
	  --format '{{.Name}} {{range .Mounts}}{{.Source}} {{end}}' \
	  | grep docker.sock | grep -v '^/dockerproxy ' || true); \
	  if [ -z "$$bad" ]; then echo "    ok: dockerproxy only"; else echo "    !!! $$bad"; rc=1; fi; \
	echo "==> qui and qbittorrent see /downloads on the SAME filesystem (ADR-0027)"; \
	a=$$(docker exec qui stat -c '%d' /downloads 2>/dev/null); \
	b=$$(docker exec qbittorrent stat -c '%d' /downloads 2>/dev/null); \
	if [ -n "$$a" ] && [ "$$a" = "$$b" ]; then echo "    ok: device $$a on both"; \
	else echo "    !!! qui=$$a qbittorrent=$$b -- hardlinks cannot cross a mount point,"; \
	     echo "        so cross-seed would silently COPY instead (0.96 TiB, ADR-0002)"; rc=1; fi; \
	echo "==> scrutiny's collector has reported within 24h (ADR-0023)"; \
	scripts/check-smart-freshness.py || rc=1; \
	echo "==> unhealthy or exited containers"; \
	u=$$(docker compose ps -a --format '{{.Name}}\t{{.Status}}' \
	     | grep -iE 'unhealthy|exited|restarting' || true); \
	  if [ -z "$$u" ]; then echo "    none"; else echo "$$u" | sed 's/^/    !!! /'; rc=1; fi; \
	exit $$rc

backup-offsite: ## Push the newest local config backup off this box (restic)
	@scripts/offsite_backup.sh --apply
