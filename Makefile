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

.PHONY: help check lint config bootstrap up down logs pull \
        pull-jellyfin update-qbittorrent measure-qbittorrent-stop \
        submodules install-hooks

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
	@# qui and ntfy are NOT linuxserver images: no root init chowns /config, so
	@# if Docker auto-creates these as root on first `up` they crash-loop on
	@# `permission denied`. ADR-0014.
	@for d in qui ntfy; do \
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
