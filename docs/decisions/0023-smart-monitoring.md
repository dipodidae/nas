# ADR-0023 — SMART monitoring is a scoped hardening exception, and it covers one disk

**Date:** 2026-09-02
**Status:** accepted
**Scoped exception to:** ADR-0001 (`cap_drop: ALL` baseline)
**Related:** ADR-0012 (one ntfy topic), ADR-0014 (config-dir ownership), ADR-0018

## The gap

This box had six monitoring layers and none of them read SMART. A dying disk
therefore announced itself as "Jellyfin is being weird" — a detection problem,
not a backup problem.

## The decision

Add `scrutiny` (omnibus image, pinned `v0.9.3-omnibus`) with a **measured**
capability and device grant, alerting to the existing `nas-alerts` ntfy topic,
UI on loopback only.

## Read this part before you trust the dashboard

**Scrutiny monitors exactly one device on this host, and a green Scrutiny
dashboard is NOT coverage of the 4.6 TB of media.**

`${SHARE_DIRECTORY}` is `/dev/sda1` on a single 9.1 TB USB disk (`Expansion
HDD`). Its bridge passes no SMART through. Re-verified 2026-09-02, from the host
_and_ from inside this container with both capabilities and `/dev/sda` passed in:

| `smartctl -d`             | result                                                           |
| ------------------------- | ---------------------------------------------------------------- |
| `sat`, `sat,12`, `sat,16` | `Read Device Identity failed: unsupported field in scsi command` |
| `usbjmicron`              | `failed: No device connected`                                    |
| `usbsunplus`              | `unsupported scsi opcode`                                        |
| `usbcypress`              | `Unknown error`                                                  |
| `scsi`                    | `SMART support is: Unavailable - device lacks SMART capability`  |

So `/dev/sda` is deliberately **not** passed through: it would grant raw access
to the data-bearing disk in exchange for zero telemetry.

The media disk's actual channels, all in `scripts/stack_watchdog.py`:

1. mount gone / remounted read-only (ext4's default on error — every \*arr
   import then fails while every container still reports healthy);
2. kernel-log I/O errors and USB resets, 6 h window;
3. **ext4's superblock error counter** (`tune2fs -l`), added with this record.
   This is the durable one: the kernel-log sweep covers 6 h and this host's
   journal retains only about 3 days (2 boots, 40 MB, measured), so an error
   older than that is invisible to both — while ext4 still reports it, because
   it is written to the superblock and survives reboots and log rotation.

`scripts/media_ops_status.py` surfaces both devices on `4eva.me/ops.html`, so
the NVMe's wear curve and the media disk's error count sit next to service
health rather than in a log nobody opens.

### The trap in the ext4 check

ext4 reports exactly one of `clean` / `not clean` / `clean with errors` /
`not clean with errors`. The check compares for **equality** with `clean`,
because a substring test for `"clean"` passes during _both_ error states — it
would have been green during the exact failure it exists to catch. A unit test
pins this (`test_classify_ext4_clean_with_errors_is_critical`). And `tune2fs`
omits `FS Error count` entirely when it is zero, so **absence is the healthy
state**; treating missing as unknown would mute the check permanently.

## The capability grant, measured rather than copied

Upstream's example asks for `SYS_RAWIO` (described as mandatory) plus
`SYS_ADMIN` (for NVMe). Measured on this host, 2026-09-02:

| grant             | result                                    |
| ----------------- | ----------------------------------------- |
| `SYS_RAWIO` alone | `NVME_IOCTL_ADMIN_CMD: Permission denied` |
| `SYS_ADMIN` alone | full attributes, `PASSED`                 |
| both              | no better than `SYS_ADMIN` alone          |
| no caps           | permission denied                         |

The only readable device here is an NVMe, and NVMe SMART goes through
`NVME_IOCTL_ADMIN_CMD`, which the kernel gates on `CAP_SYS_ADMIN`. So
**`SYS_ADMIN` alone is granted and `SYS_RAWIO` is refused** — the same
discipline that refused `FOWNER`/`FSETID` on qBittorrent (ADR-0004): do not
widen a grant that measurement says is already sufficient.

### The device must be the controller, not the namespace

`/dev/nvme0` (controller char device), **not** `/dev/nvme0n1`. `smartctl --scan`
enumerates `/dev/nvme[0-9]+`, and with only the namespace passed it returns
**empty** — the collector finds no disks, submits nothing, and the UI looks like
a normal fresh install. This is the failure mode most likely to be reintroduced
by someone "fixing" the device list, so `make check` asserts it.

Granted `:r` rather than the default `rwm`, because read was measured
sufficient.

### What this exception actually costs

`CAP_SYS_ADMIN` is close enough to root to defeat most of ADR-0001 for this one
container. `:r` blocks `write(2)` to the device node; it is **not** claimed to
block NVMe admin-command ioctls, which `CAP_SYS_ADMIN` permits by design. That
residual is the honest price. It is accepted because the container is
loopback-only, runs no user content, and the alternative is no SMART at all.

`make check` therefore asserts that scrutiny is the **only** service holding
either capability or any raw disk device. Jellyfin's `/dev/dri` is a GPU, not a
disk, and is unaffected — the assertion is disk-shaped, not devices-shaped.

## Config-dir ownership: ADR-0014, inverted

`qui` and `ntfy` need `${CONFIG_DIRECTORY}/<svc>` pre-chowned to
`${PUID}:${PGID}`, because they run as that uid with no root init.

Scrutiny is the **opposite** and must not be added to `make bootstrap`. It runs
as root (it has to — a non-root user in the container would not hold
`CAP_SYS_ADMIN` effectively) and it does **not** have `DAC_OVERRIDE`. So a
config dir owned by uid 1000 makes root fall through to the "other" permission
bits and fail:

```
./run: line 8: /opt/scrutiny/influxdb/config.yaml: Permission denied
```

Observed on first start, when the dirs had been pre-created as `tom`. Docker's
default behaviour — auto-creating a missing bind-mount source as `root:root` —
is exactly right here. Leave it alone. Fixing the ownership is strictly better
than granting `DAC_OVERRIDE` to a container that already holds `SYS_ADMIN`.

## Port 8086, not 8080

Scrutiny serves on 8080 inside the container and upstream publishes host 8080.
`qbittorrent` already owns `127.0.0.1:8080`, and three scripts
(`qbittorrent_settings_enforce.py`, `qbittorrent_stalled_kickstart.py`,
`media_ops_status.py`) authenticate there via qBittorrent's localhost
auth-bypass (ADR-0014). A collision would have broken all three.

`docker compose config` renders a duplicate host port without complaint — the
failure only appears at `up` time as a bind error on whichever container starts
second, which reads as "that service is broken" rather than "these two
conflict". `make check` now asserts no two services claim the same host port.

## Alerting

`SCRUTINY_NOTIFY_URLS` → shoutrrr `ntfy://` → the self-hosted ntfy
`nas-alerts` topic (ADR-0012). Scrutiny's own notifier stack is not used for
anything else.

It reuses the existing **write-only container publisher** (`NTFY_ARR_*`) rather
than minting a fourth ntfy account: scrutiny is less externally exposed than
the \*arrs that already share it, and a per-publisher account with no distinct
trust boundary is churn. No new `.env` variable.

Verified end to end rather than by reading Scrutiny's own success line:
`POST /api/health/notify` → Scrutiny logged "Successfully sent notifications"
→ and the message was then **read back off the ntfy topic** with the admin
account, tag `floppy_disk`, priority 4.

**Known cost, stated rather than discovered:** Scrutiny logs the full
notification URL, password included, into its container log. That is the same
exposure `docker inspect` already gives for every env-var-borne credential in
this stack (ADR-0011 discusses it for qBittorrent), to the same audience — the
host `docker` group. Accepted, not fixed. Watchtower does not do this.

## No autoheal label

A restart cannot fix a failing disk, and `autoheal` + a probe that can wedge is
how you get a restart loop (ADR-0009). Scrutiny has a healthcheck so a dead web
process is visible; nothing restarts it automatically.

## Watchtower

On the documented `WATCHTOWER_OPTOUT` list, not labelled. The omnibus image
bundles InfluxDB, and "never auto-update a database engine under its data" is
the same rule `playlist-generator-db` is opted out for (ADR-0006).

## Thresholds, and what they mean on a disk with no parity

`scripts/media_ops_status.py` grades the NVMe:

| signal                                                                    | level    | why                                          |
| ------------------------------------------------------------------------- | -------- | -------------------------------------------- |
| `critical_warning != 0`, `media_errors > 0`, SMART self-assessment FAILED | **crit** | the device is reporting trouble about itself |
| `available_spare <= available_spare_threshold`                            | **crit** | the vendor's own "replace now" line          |
| `percentage_used >= 90`                                                   | **crit** | past the modelled endurance                  |
| `percentage_used >= 80`                                                   | warn     | the slow curve, with time to act             |
| `available_spare < 20`                                                    | warn     |                                              |
| `temperature >= 70 °C`                                                    | warn     |                                              |

A failing disk marks the dashboard **degraded**, never `down`: the stack is
still serving, and `down` is reserved for "nothing works". Urgency is the
alerter's job, not the dashboard's. Status `unknown` (no `sudo` configured) is
explicitly **not** a fault, so this degrades cleanly on another host.

### Baseline on this device, 2026-09-02

`SSSTC CL1-4D256`, 256 GB, holding `${CONFIG_DIRECTORY}`, the Docker graph and
this repo, at 154 G / 232 G used:

```
Percentage Used:  42%      Available Spare: 100%   (threshold 10%)
Data Units Written: 31.4 TB   Data Units Read: 68.3 TB
Power On Hours:   9,796     Power Cycles: 6,509    Unsafe Shutdowns: 582
Media and Data Integrity Errors: 0    Error Information Log Entries: 0
```

42 % consumed with 100 % spare and zero media errors is "wearing, not failing".
It is worth trending precisely because there is no redundancy underneath it.

### The 582 unsafe shutdowns do not correlate with the Jellyfin OOM kills

Asked and answered, because it would have been a second finding. It is not one,
for two independent reasons:

1. **Mechanism.** A Linux OOM kill terminates a process; the host keeps running
   and the NVMe is never power-cycled. Only a hard reset, hang or power loss
   increments `Unsafe Shutdowns`. The two cannot cluster.
2. **The counter predates this box.** 6,509 power cycles across 9,796 power-on
   hours is one cycle per 1.5 h of uptime — nothing like a server's duty cycle,
   and consistent with an OEM laptop drive's sleep/resume history before it
   arrived here. 582 unsafe of 6,509 is 8.9 %.

It could not have been tested historically anyway: the journal retains ~3 days,
which is itself worth knowing, since `stack_watchdog.py` reads `journalctl -k`
for OOM kills and ADR-0008's evidence base has that same horizon.
