# Legacy / Pi-era scripts

These scripts were written for an older Raspberry Pi host that this stack used
to run on. They are kept for reference only — **do not run them on the current
MS01 host**. CLAUDE.md notes that Pi-era resource limits and host-kernel tuning
were intentionally removed, and re-applying them would regress the live
configuration.

What's in here:

- `99-qbittorrent-sysctl.conf`, `qbittorrent-optimized.conf` — Pi-era kernel
  tuning + qBittorrent ini snapshots.
- `qbittorrent-scheduler.py` — bandwidth scheduler tuned to Pi limits.
- `install-qbittorrent-optimization.sh`, `apply-all-fixes.sh`,
  `make-advanced-tuning-persistent.sh`, `make-io-persistent.sh`,
  `optimize-disk-io.sh`, `optimize-nginx-workers.sh`,
  `ultimate-performance-boost.sh`, `advanced-system-tuning.sh`,
  `check-streaming-status.sh`, `INSTALL_GUIDE.sh` — host-level shell
  installers that mutate sysctl / nginx / systemd.
- `EXECUTION_PLAN.md`, `INSTALLATION_COMPLETE.txt`, `QBITTORRENT_OPTIMIZATION.md`,
  `QUICK_START.md`, `README_QBITTORRENT.md` — paired docs for the above.

If a future host actually needs this kind of tuning again, treat these as a
historical reference and re-derive against current hardware/kernel.
