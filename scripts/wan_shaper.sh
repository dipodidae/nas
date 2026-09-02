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
SHAPE_MBIT="${SHAPE_MBIT:-28}"
LAN_CIDR="${LAN_CIDR:-192.168.2.0/24}"
# Well above any real LAN speed, so the LAN class is effectively unshaped while
# still living under one HTB root.
LAN_CEIL_MBIT="${LAN_CEIL_MBIT:-10000}"

die() { printf 'wan_shaper: %s\n' "$1" >&2; exit 2; }

require_root() {
  [ "$(id -u)" -eq 0 ] || die "must run as root (tc needs CAP_NET_ADMIN)"
}

clear_qdisc() {
  tc qdisc del dev "$WAN_IF" root 2>/dev/null || true
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

status_qdisc() {
  printf '=== qdisc on %s ===\n' "$WAN_IF"
  tc -s qdisc show dev "$WAN_IF"
  printf '\n=== classes (1:10 LAN unshaped / 1:20 internet shaped at %s Mbit) ===\n' "$SHAPE_MBIT"
  tc -s class show dev "$WAN_IF"
  printf '\n=== filters ===\n'
  tc filter show dev "$WAN_IF"
}

case "${1:-}" in
  apply)  require_root; apply_qdisc; printf 'wan_shaper: shaping %s internet egress at %s Mbit (LAN %s exempt)\n' "$WAN_IF" "$SHAPE_MBIT" "$LAN_CIDR" ;;
  clear)  require_root; clear_qdisc; printf 'wan_shaper: cleared, %s back to kernel default\n' "$WAN_IF" ;;
  status) status_qdisc ;;
  *)      die "usage: $0 {apply|clear|status}" ;;
esac
