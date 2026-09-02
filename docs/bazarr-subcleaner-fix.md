# Bazarr's post-processing was failing on every single subtitle download

Found and fixed 2026-09-02, while investigating "Poirot has such shitty subs".

## Symptom

Bazarr's API returned `HTTP 204` for every manual subtitle download and **no
file appeared on disk**. Its own history recorded the downloads — including
`English subtitles manually downloaded from subdl with a score of 99` and
`English subtitles synchronization ended with an offset of 8.98 seconds` — so
the search, the download and even the ffsubsync pass all worked. The subtitle
was then discarded.

## Cause

`postprocessing_cmd` is `python3 /opt/subcleaner/subcleaner.py "{{subtitles}}" -s`,
and `/opt/subcleaner` is bind-mounted **read-only**:

```yaml
- ${CONFIG_DIRECTORY}/subcleaner:/opt/subcleaner:ro
```

subcleaner writes its default config into its own directory on first run:

```python
config_file = home_dir.joinpath("subcleaner.conf")
if not config_file.is_file():
    config_file.write_text(home_dir.joinpath("default_config", "subcleaner.conf").read_text())
```

The checkout shipped `default_config/subcleaner.conf` but no top-level
`subcleaner.conf`, so every invocation tried to create one and died:

```
OSError: [Errno 30] Read-only file system: '/opt/subcleaner/subcleaner.conf'
```

Bazarr logged that as `ERROR (post_processing:40)` and dropped the subtitle.
**This affected every show, not just Poirot** — a movie
(`/data/movies/Character (1997)/...`) failed identically in the same log window.

## The second failure, after fixing the first

Providing `subcleaner.conf` moved the error one step along:

```
OSError: [Errno 30] Read-only file system: 'opt/subcleaner/logs'
OSError: [Errno 30] Read-only file system: '/opt/subcleaner/logs/subcleaner.log'
```

subcleaner resolves a **relative** `log_dir` against its own (read-only) home,
and creating the directory on the host does not help, because appending to a
file inside a `:ro` mount is refused regardless. The fix is an **absolute**
`log_dir` pointing at Bazarr's writable volume:

```ini
log_dir = /config/log
```

confirmed by the code path:

```python
if not log_dir.is_absolute():
    log_dir = home_dir.joinpath(log_dir)
```

A third, smaller trap: a `subcleaner.log` created by a `docker exec` runs as
**root**, and Bazarr runs as `abc` (uid 1000), so it then fails with
`Permission denied to: "/config/log/subcleaner.log"`. Delete it and let Bazarr
create its own.

## Verification

```
$ docker exec bazarr python3 /opt/subcleaner/subcleaner.py "<a real .srt>"
subcleaner finished successfully. 1 files cleaned.

$ docker logs --since 3m bazarr | grep -c 'Read-only file system'
0
```

Before the fix that count was **18** in a four-minute window.

## Why the conf is tracked in the repo

`bazarr/subcleaner.conf` is committed and bind-mounted read-only over the path
inside the read-only checkout. Same reasoning as the SWAG proxy-confs
(ADR-0022): a fix that lives only in the gitignored config directory is one
`rm -rf` away from silently regressing — and this failure mode is **invisible**.
Nothing alerts on it. Subtitles simply never improve, and the only visible
symptom is a human saying the subtitles are bad.

## The lesson worth keeping

Bazarr answered `204`, its history said "downloaded", and its scores were
excellent. Every layer reported success except the one that wrote the file.
This is the repo's own rule again: _when a check passes, ask whether it proves
the property you care about or just the component that carries it._ The property
was "a subtitle file exists on disk"; every available signal proved something
else.
