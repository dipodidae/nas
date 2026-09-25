# Decision records

One file per decision that constrains how this stack may be changed. They exist
because the reasoning used to live in `docker-compose.yml` as multi-line
narrative comments, which does not survive a refactor and cannot be linked to.

The compose modules now carry only a one-line **invariant** plus a pointer to
the ADR here. If you are about to change something and the YAML says
`INVARIANT:`, read the ADR before changing it.

| ADR                                                              | Subject                                                                                                                     |
| ---------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| [0000](0000-compose-layout.md)                                   | One project, many files: `include`, network-once, `extends` vs anchors                                                      |
| [0001](0001-hardening-baseline.md)                               | The hardening baseline, and why Pi-era resource limits were removed                                                         |
| [0002](0002-single-mount-data-hardlinks.md)                      | `${SHARE_DIRECTORY}:/data` — hardlinks cannot cross a mount point                                                           |
| [0003](0003-lidarr-data-mount-staged.md)                         | Lidarr's `/data` is staged but unused; the editor endpoint wiped TrackFiles                                                 |
| [0004](0004-qbittorrent-cap-kill.md)                             | qBittorrent needs `CAP_KILL` — the real root cause of the crash cycle                                                       |
| [0005](0005-qbittorrent-pinned-tag.md)                           | Pinned tag (floor 5.2.2), stable hostname, and the stale-lock init script                                                   |
| [0006](0006-watchtower-opt-outs.md)                              | Watchtower's recreate is not atomic; who is opted out and why                                                               |
| [0007](0007-qbittorrent-memory-cap.md)                           | qBittorrent `mem_limit: 4g` — libtorrent mmap and the 21.1GB cgroup peak                                                    |
| [0008](0008-jellyfin-memory-mitigations.md)                      | Jellyfin memory: NOT scan-correlated; `mem_limit` is now defence in depth                                                   |
| [0009](0009-slskd-healthcheck.md)                                | slskd's healthcheck is deliberately Soulseek-independent                                                                    |
| [0010](0010-autoheal-timeouts.md)                                | autoheal's own stop timeout, and the t+150s pile-up                                                                         |
| [0011](0011-qbittorrent-credentials.md)                          | `QBITTORRENT_USER`/`PASS` belong in `.env`, not in the container                                                            |
| [0012](0012-ntfy-alerting.md)                                    | Self-hosted ntfy, deny-all, and the host-down blind spot                                                                    |
| [0013](0013-dockerproxy-sole-socket-holder.md)                   | Only dockerproxy touches `/var/run/docker.sock`                                                                             |
| [0014](0014-qui-and-non-lsio-images.md)                          | qui replaces qBittorrent's public WebUI; pre-chowned config dirs                                                            |
| [0015](0015-bazarr-no-data-mount.md)                             | Bazarr deliberately has no `/data` mount                                                                                    |
| [0016](0016-jellyfin-paths-are-load-bearing.md)                  | Jellyfin's volume mappings are load-bearing and must not change                                                             |
| [0017](0017-cleanuparr-armed.md)                                 | Cleanuparr is an armed deletion engine; three modules stay off                                                              |
| [0018](0018-capability-gaps.md)                                  | Known gap: playlist-generator and its db do not drop capabilities                                                           |
| [0019](0019-no-vpn-home-ip.md)                                   | No VPN — P2P egresses over the home IP                                                                                      |
| [0020](0020-watchtower-replaced-and-demoted.md)                  | Watchtower replaced with a maintained fork and demoted to monitor-only                                                      |
| [0021](0021-nginx-cap-kill.md)                                   | An nginx whose master and workers differ in uid needs `CAP_KILL`                                                            |
| [0022](0022-proxy-confs-are-tracked.md)                          | Proxy-confs are tracked in-repo; the conf routes, the label only documents                                                  |
| [0023](0023-smart-monitoring.md)                                 | SMART monitoring: a scoped `SYS_ADMIN` exception, covering ONE disk                                                         |
| [0024](0024-diun-version-aware-notification.md)                  | Diun watches image repos from a generated manifest; pinned tags visible                                                     |
| [0025](0025-watchtower-retired.md)                               | Watchtower retired; dockerproxy narrowed to what autoheal alone needs                                                       |
| [0026](0026-slskd-start-period-and-alert-noise.md)               | slskd start_period must outlast a forced rescan; a startup must not page                                                    |
| [0027](0027-qui-cross-seed-prerequisites.md)                     | qui cross-seed: the filesystem grant is the risk; ext4 has no reflink                                                       |
| [0028](0028-beszel-trend-lines.md)                               | Beszel trend lines; its agent must NOT use host networking here                                                             |
| [0029](0029-huntarr-rejected.md)                                 | Huntarr rejected: archived upstream, unauthenticated \*arr API-key leak                                                     |
| [0030](0030-streamystats.md)                                     | Streamystats: 3 containers, own VectorChord DB (not pgvector), API-only                                                     |
| [0031](0031-considered-and-rejected.md)                          | Compose UIs, second notifiers/cleaners, socket mounts, Dozzle, Gatus                                                        |
| [0032](0032-alert-noise-ownership.md)                            | The watchdog owns indexer/\*arr health alerting; onHealthIssue off                                                          |
| [0033](0033-ntfy-topic-taxonomy.md)                              | Six ntfy lanes: priority carries severity, topic carries audience                                                           |
| [0034](0034-one-door-tinyauth.md)                                | One tinyauth door for every browser-only surface; sync clients excluded                                                     |
| [0035](0035-jellyfin-12-upgrade.md)                              | Jellyfin 12.0: `CAP_KILL`, the tag regex, legacy auth, and `encoding.xml`                                                   |
| [0036](0036-tinyauth-52-auth-module-conflict.md)                 | tinyauth v5.2.0 denies SWAG's `proxy.conf` headers; its DB upgrade is one-way                                               |
| [0037](0037-backup-completeness.md)                              | The nightly archive discovers its services and snapshots the WAL databases                                                  |
| [0038](0038-artist-art-and-image-providers.md)                   | Artist images from Deezer, verified against the albums on disk                                                              |
| [0039](0039-trickplay-writes-to-the-media-tree.md)               | Trickplay tiles live with the media; the `:ro` mount made every run a no-op                                                 |
| [0040](0040-nfs-lockd-random-port.md)                            | NFS `lockd` drew a random port, took Jellyfin's 7359, and 502'd it for 20 h                                                 |
| [0041](0041-cap-kill-is-not-enough-without-grace.md)             | `CAP_KILL` without a `stop_grace_period` is still a SIGKILL — amends ADR-0035                                               |
| [0042](0042-postgres-18-moved-the-data-directory.md)             | Postgres 18 refuses the old bind mount, and a pinned bump that never edits compose is a silent no-op                        |
| [0043](0043-adguard-home-lan-dns.md)                             | AdGuard binds one address (systemd-resolved owns `0.0.0.0:53`); the host does not use it                                    |
| [0044](0044-navidrome-subsonic-path-scope.md)                    | Navidrome behind the door, `/rest` path-scoped open for Subsonic clients                                                    |
| [0045](0045-unrouted-hostnames-must-error.md)                    | An unrouted hostname answers 404; `site-confs/default.conf` is tracked                                                      |
| [0046](0046-album-art-two-stage-and-miss-memory.md)              | Album art: a retry with no memory starved its own batch, and `-t 25` hid 80% of the gap                                     |
| [0047](0047-pre-upgrade-backup-is-downtime.md)                   | The pre-upgrade backup stopped jellyfin for 17 min to copy 19 GB of re-fetchable artwork                                    |
| [0048](0048-a-zero-byte-wal-is-a-receipt.md)                     | A 0-byte `-wal` is a checkpoint receipt; the check now asserts bytes, not filenames                                         |
| [0049](0049-lidarr-triggers-navidrome-scans.md)                  | Lidarr triggers Navidrome's scan; only `OnReleaseImport`/`OnRename` call `Update()`                                         |
| [0050](0050-playlists-reach-phones-through-navidrome.md)         | Playlists out of Jellyfin and into Navidrome; a Subsonic `path` is synthesised from tags                                    |
| [0051](0051-jellyfin-drops-music.md)                             | Jellyfin drops music; a deleted library orphans its items, and SQLite needs a VACUUM                                        |
| [0052](0052-a-bind-mounted-socket-detaches-on-daemon-restart.md) | A bind-mounted Docker socket detaches when the daemon restarts; dockerproxy 503s and autoheal crash-loops                   |
| [0053](0053-navidrome-plugins-and-audiomuse.md)                  | Navidrome plugins + AudioMuse-AI owns Instant Mix; two workers, not one bigger one (64.6× realtime)                         |
| [0054](0054-bazarr-accuracy-and-subtitle-audit.md)               | Bazarr for accuracy (OpenSubtitles back, framerate sync, NL + EN) + an hourly audit that checks subtitles against the audio |
