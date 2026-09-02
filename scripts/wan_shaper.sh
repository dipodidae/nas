#!/usr/bin/env bash
# Shape this host's internet-bound egress with CAKE, so the modem's queue never
# fills and loaded latency stays low.
#
# Why this exists
# ---------------
# The uplink is ~31 Mbps and the router is a KPN Experia Box with no SQM of any
# kind, so the only queue on the path was the modem's — a dumb FIFO that
# BitTorrent kept permanently full. Measured 2026-09-02: 5% packet loss,
# 127 ms latency spikes and 25.8 ms jitter while seeding, versus 0% / 18 ms /
# 1.8 ms idle. That collapses TCP throughput for everything else on the link and
# is what made remote Jellyfin playback stutter. See docs/jellyfin-playback-audit.md.
#
# Shaping slightly below the real uplink rate moves the bottleneck into a queue
# we control. CAKE then keeps standing queue near-zero and isolates flows, so no
# single bulk transfer can monopolise it — and that holds for cloud sync,
# backups and anything else added later, not just BitTorrent.
#
# Bulk marking
# ------------
# Shaping alone only bounds latency; it does not decide who gets the bandwidth.
# CAKE's flow fairness would hand a torrent with dozens of flows most of the
# link regardless of any arithmetic budget. So qBittorrent and slskd are marked
# CS1, which lands them in CAKE's Bulk tin: throttled hard whenever another tin
# is active, free to use the whole pipe when nothing else wants it. That yields
# automatically to one remote stream or five, instead of reserving a fixed
# number of Mbit for a viewer who may not exist.
#
# (There is no VPN on this host as of 2026-07-27, so these are the real packets
# and can be marked directly. If a tunnel is ever reintroduced, the mark has to
# move to the tunnel's OUTER packets — WireGuard does not carry the inner DSCP.)
#
# The LAN exemption is the non-obvious part
# -----------------------------------------
# enp88s0 carries BOTH LAN and internet traffic — the gateway and every LAN
# client are on the same 192.168.2.0/24. Shaping the whole interface would cap
# LAN traffic at the uplink rate too, and this box serves 40 GB BluRay remuxes
# that direct-play at ~48 Mbps. So: an HTB root splits egress into an unshaped
# LAN class and a shaped internet class, and only the latter gets CAKE.
#
# Usage
# -----
#   sudo scripts/wan_shaper.sh apply     # install the qdisc
#   sudo scripts/wan_shaper.sh status    # show it, with live stats
#   sudo scripts/wan_shaper.sh check     # exit 0 only if shaping AND marking
#   sudo scripts/wan_shaper.sh clear     # back to the kernel default
#
# Rate changes: edit SHAPE_MBIT, re-run `apply`. It is idempotent — apply always
# tears down and rebuilds. Re-measure with a multi-stream upload against
# /sys/class/net/<if>/statistics/tx_bytes before changing it; do not trust a
# speedtest's own number, and do not trust qBittorrent's.

set -euo pipefail
IFS=$'\n\t'

WAN_IF="${WAN_IF:-enp88s0}"
# ~90% of the 31 Mbps measured at the NIC counter on 2026-09-02. Shaping must sit
# below the real rate or the modem, not us, stays the bottleneck.
#
# 85% (26 Mbit) was A/B'd against this and REJECTED on the data: across 18
# samples the residual latency spikes do not correlate with our own upload
# (r = +0.20), appear at near-idle just as at full load (worst max 44.9 ms below
# 5 Mbps vs 61.6 ms above 10 Mbps), and 26 Mbit's worst case was no better.
# Packet loss is 0% at both. The spikes are path variance to the probe target,
# not our queue, so shaping harder costs 2 Mbps and buys nothing. Re-measure
# before revisiting; the criterion is loss and load-correlation, not max RTT.
SHAPE_MBIT="${SHAPE_MBIT:-28}"
LAN_CIDR="${LAN_CIDR:-192.168.2.0/24}"
# Well above any real LAN speed, so the LAN class is effectively unshaped while
# still living under one HTB root.
LAN_CEIL_MBIT="${LAN_CEIL_MBIT:-10000}"
# Containers whose egress is bulk: they should surrender the link to anything
# else that wants it. Marked CS1 so CAKE files them in its Bulk tin, which is
# held to a 6.25% threshold when other tins are active but may use the whole
# pipe when they are not. Names, not IPs — Docker reassigns IPs on recreate, so
# they are resolved at apply time and this script is re-run hourly from cron.
BULK_CONTAINERS=(qbittorrent slskd)
DSCP_COMMENT="wan_shaper-bulk"

die() { printf 'wan_shaper: %s\n' "$1" >&2; exit 2; }

require_root() {
  [ "$(id -u)" -eq 0 ] || die "must run as root (tc needs CAP_NET_ADMIN)"
}

clear_qdisc() {
  tc qdisc del dev "$WAN_IF" root 2>/dev/null || true
}

clear_marks() {
  # Delete every rule we previously added, identified by its comment, so this
  # is idempotent and leaves nothing behind for anyone else's rules.
  while iptables -t mangle -S POSTROUTING | grep -q -- "--comment $DSCP_COMMENT"; do
    local n
    n=$(iptables -t mangle -L POSTROUTING --line-numbers -n | awk -v c="$DSCP_COMMENT" '$0 ~ c {print $1; exit}')
    [ -n "$n" ] || break
    iptables -t mangle -D POSTROUTING "$n"
  done
}

apply_marks() {
  clear_marks
  local ip name marked=0
  for name in "${BULK_CONTAINERS[@]}"; do
    ip=$(docker inspect "$name" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null || true)
    if [ -z "$ip" ]; then
      printf 'wan_shaper: %s not running, no bulk mark applied\n' "$name" >&2
      continue
    fi
    # mangle POSTROUTING runs BEFORE nat POSTROUTING, so the source is still the
    # container address here; Docker's SNAT afterwards rewrites addresses but
    # leaves the DSCP field alone, so the mark survives to the wire.
    iptables -t mangle -A POSTROUTING -s "$ip" -j DSCP --set-dscp-class CS1 \
      -m comment --comment "$DSCP_COMMENT"
    printf 'wan_shaper: %s (%s) -> DSCP CS1 (CAKE Bulk tin)\n' "$name" "$ip"
    marked=$((marked + 1))
  done
  [ "$marked" -gt 0 ] || printf 'wan_shaper: WARNING no bulk containers marked\n' >&2
}

apply_qdisc() {
  command -v tc >/dev/null || die "tc not found (install iproute2)"
  modprobe sch_cake 2>/dev/null || true
  tc qdisc show dev "$WAN_IF" | grep -q cake && printf 'wan_shaper: replacing existing shaper\n'

  clear_qdisc

  # class 1:10 = LAN, unshaped.  class 1:20 = internet, shaped + CAKE.
  # `default 20` sends anything the filter does not match (i.e. internet-bound)
  # into the shaped class, which is the safe direction to fail.
  tc qdisc add dev "$WAN_IF" root handle 1: htb default 20
  tc class add dev "$WAN_IF" parent 1:  classid 1:1  htb rate "${LAN_CEIL_MBIT}mbit"
  tc class add dev "$WAN_IF" parent 1:1 classid 1:10 htb rate "${LAN_CEIL_MBIT}mbit" ceil "${LAN_CEIL_MBIT}mbit"
  tc class add dev "$WAN_IF" parent 1:1 classid 1:20 htb rate "${SHAPE_MBIT}mbit"    ceil "${SHAPE_MBIT}mbit"

  # fq_codel on the LAN side costs nothing and keeps a local bulk transfer from
  # head-of-line blocking interactive LAN traffic.
  tc qdisc add dev "$WAN_IF" parent 1:10 handle 10: fq_codel
  # No `nat` option: this is an end host, not the router, so conntrack lookups
  # would be pointless work. Defaults (diffserv3 + triple-isolate) are right —
  # the point is bounded queuing delay, while the per-application bandwidth
  # split comes from qBittorrent's own upload cap.
  tc qdisc add dev "$WAN_IF" parent 1:20 handle 20: cake bandwidth "${SHAPE_MBIT}mbit" ethernet

  # LAN-destined traffic bypasses the shaper entirely. Without this, 40 GB
  # remuxes direct-playing on the LAN would be throttled to the uplink rate.
  tc filter add dev "$WAN_IF" protocol ip parent 1: prio 1 u32 \
    match ip dst "$LAN_CIDR" flowid 1:10
  # Link-local / multicast (mDNS, SSDP — Jellyfin's LAN discovery) too.
  tc filter add dev "$WAN_IF" protocol ip parent 1: prio 1 u32 \
    match ip dst 224.0.0.0/4 flowid 1:10
}

check_health() {
  # Answer the question callers actually care about — "is internet egress being
  # shaped AND prioritised right now" — rather than the easier one, "does a
  # qdisc exist". Those differ: the DSCP rules live only in this script and any
  # netfilter reload (a firewall service running iptables-restore, a Docker
  # network change, manual rule surgery) drops them without touching the qdisc.
  # A caller that checks only the qdisc would then report healthy while torrents
  # had silently stopped yielding.
  local problems=0

  if ! tc qdisc show dev "$WAN_IF" | grep -q cake; then
    printf 'wan_shaper: FAIL no CAKE qdisc on %s\n' "$WAN_IF"
    problems=$((problems + 1))
  elif ! tc qdisc show dev "$WAN_IF" | grep -q "bandwidth ${SHAPE_MBIT}Mbit"; then
    # A stale rate is a shaper that has silently stopped working: shaped above
    # the real line rate, the modem is the bottleneck again and CAKE never
    # queues anything.
    printf 'wan_shaper: FAIL CAKE bandwidth is not %sMbit (line re-provisioned? re-measure)\n' "$SHAPE_MBIT"
    problems=$((problems + 1))
  fi

  local want found
  want=${#BULK_CONTAINERS[@]}
  found=$(iptables -t mangle -S POSTROUTING 2>/dev/null | grep -c -- "$DSCP_COMMENT" || true)
  if [ "$found" -ne "$want" ]; then
    # Exact, not ">=": too few means torrents are not yielding, and too many
    # means the teardown is not matching and rules are accumulating on every
    # apply — which is how this was found. Either is a defect.
    printf 'wan_shaper: FAIL %s DSCP bulk marks present, expected exactly %s\n' "$found" "$want"
    problems=$((problems + 1))
  fi

  [ "$problems" -eq 0 ] && printf 'wan_shaper: OK shaping %sMbit, %s bulk marks\n' "$SHAPE_MBIT" "$found"
  return $((problems > 0))
}

status_qdisc() {
  printf '=== qdisc on %s ===\n' "$WAN_IF"
  tc -s qdisc show dev "$WAN_IF"
  printf '\n=== classes (1:10 LAN unshaped / 1:20 internet shaped at %s Mbit) ===\n' "$SHAPE_MBIT"
  tc -s class show dev "$WAN_IF"
  printf '\n=== filters ===\n'
  tc filter show dev "$WAN_IF"
  printf '\n=== DSCP bulk marks ===\n'
  iptables -t mangle -S POSTROUTING | grep -- "$DSCP_COMMENT" || printf '  (none)\n' 
}

case "${1:-}" in
  apply)  require_root; apply_qdisc; apply_marks; printf 'wan_shaper: shaping %s internet egress at %s Mbit (LAN %s exempt)\n' "$WAN_IF" "$SHAPE_MBIT" "$LAN_CIDR" ;;
  clear)  require_root; clear_marks; clear_qdisc; printf 'wan_shaper: cleared, %s back to kernel default\n' "$WAN_IF" ;;
  status) status_qdisc ;;
  check)  check_health ;;
  *)      die "usage: $0 {apply|clear|status|check}" ;;
esac
