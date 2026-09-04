#!/usr/bin/env bash
#
# check-swag-conf-drift.sh -- assert every SWAG conf tracked in this repo is
# byte-identical to the one the running nginx is actually serving.
#
# WHY THIS EXISTS. ADR-0022 mounts each conf FILE individually (a read-only
# mount over the whole proxy-confs directory breaks SWAG's startup, which
# rewrites 372 *.conf.sample files into it). Docker binds a single file by
# INODE, so anything that REPLACES the host file instead of rewriting it in
# place silently detaches the mount: `git checkout`, `git revert`, `git stash
# pop`, prettier's writer, `sed -i`, and every editor that writes-then-renames.
# The container keeps serving the old inode, `nginx -t` passes, `nginx -s
# reload` changes nothing, and `git diff` is clean -- so the repo, the linter
# and the invariant checker all agree the change is deployed when it is not.
#
# Measured 2026-09-04: a `git checkout` of ongehoord.subdomain.conf left the
# container serving the previous revision. The tell was `nginx -T` showing
# comment text that no longer existed in the repo.
#
# Under ADR-0022 the conf IS the mechanism, so this is not cosmetic drift: it
# is the difference between a route having its auth door and only appearing to.
# The fix is always a RECREATE, never a reload:  make swag-apply
#
# Exit codes follow the repo convention (AGENTS.md):
#   0  every tracked conf matches
#   1  one or more differ, or a tracked conf is missing in the container
#   2  fatal -- swag is not running, so nothing could be compared
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

if ! docker inspect swag >/dev/null 2>&1; then
  echo "    !!! swag has no container; cannot compare confs (ADR-0006)" >&2
  exit 2
fi

rc=0
n=0
# The apex conf is included because it now carries a path-scoped auth door of
# its own (/ops.html, /ops-status.json), so it is exactly as load-bearing as a
# proxy-conf -- and it arrives under a DIFFERENT name (site-confs/root.conf),
# which is why the mapping below is explicit rather than derived. ADR-0034.
for f in swag/proxy-confs/*.conf swag/*.conf \
         webapps/4eva-rootpage/4eva-rootpage.root.conf; do
  [ -f "$f" ] || continue
  name="$(basename "$f")"
  case "$f" in
    swag/proxy-confs/*)                          target="/config/nginx/proxy-confs/$name" ;;
    webapps/4eva-rootpage/*.root.conf)           target="/config/nginx/site-confs/root.conf" ;;
    *)                                           target="/config/nginx/$name" ;;
  esac
  want="$(sha256sum "$f" | cut -d' ' -f1)"
  got="$(docker exec swag sha256sum "$target" 2>/dev/null | cut -d' ' -f1)"
  n=$((n + 1))
  if [ -z "$got" ]; then
    echo "    !!! $name is tracked but ABSENT at $target -- the route it serves" >&2
    echo "        is either unpublished or served by an untracked conf" >&2
    rc=1
  elif [ "$want" != "$got" ]; then
    echo "    !!! $name DRIFTED: repo ${want:0:12} != container ${got:0:12}" >&2
    echo "        the bind mount is detached (the host file was replaced, not" >&2
    echo "        rewritten). nginx is serving the OLD conf. Fix: make swag-apply" >&2
    rc=1
  fi
done

[ $rc -eq 0 ] && echo "    ok: $n tracked confs byte-identical in the container"
exit $rc
