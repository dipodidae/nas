# Decision records

One file per decision that constrains how this stack may be changed. They exist
because the reasoning used to live in `docker-compose.yml` as multi-line
narrative comments, which does not survive a refactor and cannot be linked to.

The compose modules now carry only a one-line **invariant** plus a pointer to
the ADR here. If you are about to change something and the YAML says
`INVARIANT:`, read the ADR before changing it.

| ADR                                             | Subject                                                                     |
| ----------------------------------------------- | --------------------------------------------------------------------------- |
| [0000](0000-compose-layout.md)                  | One project, many files: `include`, network-once, `extends` vs anchors      |
| [0001](0001-hardening-baseline.md)              | The hardening baseline, and why Pi-era resource limits were removed         |
| [0002](0002-single-mount-data-hardlinks.md)     | `${SHARE_DIRECTORY}:/data` — hardlinks cannot cross a mount point           |
| [0003](0003-lidarr-data-mount-staged.md)        | Lidarr's `/data` is staged but unused; the editor endpoint wiped TrackFiles |
| [0004](0004-qbittorrent-cap-kill.md)            | qBittorrent needs `CAP_KILL` — the real root cause of the crash cycle       |
| [0005](0005-qbittorrent-pinned-tag.md)          | Pinned tag (floor 5.2.2), stable hostname, and the stale-lock init script   |
| [0006](0006-watchtower-opt-outs.md)             | Watchtower's recreate is not atomic; who is opted out and why               |
| [0007](0007-qbittorrent-memory-cap.md)          | qBittorrent `mem_limit: 4g` — libtorrent mmap and the 21.1GB cgroup peak    |
| [0008](0008-jellyfin-memory-mitigations.md)     | Jellyfin's three leak mitigations, all stopgaps                             |
| [0009](0009-slskd-healthcheck.md)               | slskd's healthcheck is deliberately Soulseek-independent                    |
| [0010](0010-autoheal-timeouts.md)               | autoheal's own stop timeout, and the t+150s pile-up                         |
| [0011](0011-qbittorrent-credentials.md)         | `QBITTORRENT_USER`/`PASS` belong in `.env`, not in the container            |
| [0012](0012-ntfy-alerting.md)                   | Self-hosted ntfy, deny-all, and the host-down blind spot                    |
| [0013](0013-dockerproxy-sole-socket-holder.md)  | Only dockerproxy touches `/var/run/docker.sock`                             |
| [0014](0014-qui-and-non-lsio-images.md)         | qui replaces qBittorrent's public WebUI; pre-chowned config dirs            |
| [0015](0015-bazarr-no-data-mount.md)            | Bazarr deliberately has no `/data` mount                                    |
| [0016](0016-jellyfin-paths-are-load-bearing.md) | Jellyfin's volume mappings are load-bearing and must not change             |
| [0017](0017-cleanuparr-armed.md)                | Cleanuparr is an armed deletion engine; three modules stay off              |
| [0018](0018-capability-gaps.md)                 | Known gap: playlist-generator and its db do not drop capabilities           |
| [0019](0019-no-vpn-home-ip.md)                  | No VPN — P2P egresses over the home IP                                      |
| [0020](0020-watchtower-replaced-and-demoted.md) | Watchtower replaced with a maintained fork and demoted to monitor-only      |
| [0021](0021-nginx-cap-kill.md)                  | An nginx whose master and workers differ in uid needs `CAP_KILL`            |
| [0022](0022-proxy-confs-are-tracked.md)         | Proxy-confs are tracked in-repo; the conf routes, the label only documents  |
| [0023](0023-smart-monitoring.md)                | SMART monitoring: a scoped `SYS_ADMIN` exception, covering ONE disk         |
| [0024](0024-diun-version-aware-notification.md) | Diun watches image repos from a generated manifest; pinned tags visible     |
