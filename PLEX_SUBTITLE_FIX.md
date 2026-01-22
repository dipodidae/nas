# Plex Subtitle Download Fix

## Problem

Plex removed native subtitle downloading in recent versions (v1.42+). The built-in OpenSubtitles and other subtitle agents no longer work.

## Solution

Use **Bazarr** for automatic subtitle downloads. Bazarr is specifically designed for this purpose and integrates with both Plex, Sonarr, and Radarr.

## What Was Done

### 1. Enabled Plex Integration in Bazarr

- Updated `/mnt/drive/.docker-config/bazarr/config/config.yaml`
- Set `use_plex: true` in general settings
- Configured Plex connection with your PLEX_TOKEN
- Backup created at `config.yaml.bak`

### 2. Restarted Bazarr

```bash
docker compose restart bazarr
```

## How to Use Bazarr for Subtitles

### Automatic Subtitle Downloads

Bazarr automatically scans your Plex library (via Sonarr/Radarr) and downloads missing subtitles based on your preferences.

### Manual Subtitle Search

1. Access Bazarr web UI: http://localhost:6767 (or https://bazarr.4eva.me)
2. Browse to Series or Movies
3. Click on a title
4. Click "Manual Search" for specific episodes/movies
5. Select and download subtitles

### Configure Subtitle Preferences

1. Go to Bazarr Settings → Languages
2. Add your preferred subtitle languages
3. Set language profiles (e.g., English only, or English + Dutch)
4. Enable "Single Language" or "Multi Language" mode

### Current Configuration

- **Sonarr Integration**: ✓ Enabled (for TV shows)
- **Radarr Integration**: ✓ Enabled (for movies)
- **Plex Integration**: ✓ Enabled (for library scanning)
- **Subtitle Providers**: yifysubtitles, wizdom, podnapisi, subs4free, tvsubtitles

### Recommended Provider: OpenSubtitles

For better subtitle availability:

1. Go to Bazarr Settings → Providers
2. Add "OpenSubtitles.com" (free account required)
3. Sign up at https://www.opensubtitles.com/
4. Add your username/password to Bazarr

## Viewing Subtitles in Plex

Once Bazarr downloads subtitles:

1. Subtitles are saved next to your media files
2. Plex automatically detects them
3. Enable subtitles in Plex player (CC button)
4. No additional configuration needed in Plex

## Automation

Bazarr will:

- Scan for new media every 60 minutes (configurable)
- Download missing subtitles automatically
- Upgrade low-quality subtitles after 7 days
- Monitor Sonarr/Radarr for new releases

## Troubleshooting

### Bazarr Not Finding Media

1. Check Sonarr/Radarr are connected: Settings → Sonarr/Radarr
2. Verify API keys match (already configured)
3. Trigger manual sync: Settings → Sonarr → Test / Sync

### Subtitles Not Appearing in Plex

1. Refresh metadata in Plex for the specific item
2. Check file permissions (should be readable by PUID 1001)
3. Verify subtitle file exists next to video file

### No Subtitle Providers Working

1. Add OpenSubtitles.com account (recommended)
2. Check provider status: Settings → Providers
3. Some providers may be rate-limited or down

## Files Modified

- `/mnt/drive/.docker-config/bazarr/config/config.yaml` (backup: `config.yaml.bak`)
- Script created: `/home/tom/nas/scripts/enable_bazarr_plex.py`

## Next Steps

1. Wait for Bazarr to fully start (~30 seconds)
2. Access Bazarr at http://localhost:6767
3. Go to Settings → Plex and verify connection shows "Connected"
4. Go to Settings → Languages and configure your preferred languages
5. Optionally add OpenSubtitles.com provider for better coverage

## Resources

- Bazarr Documentation: https://wiki.bazarr.media/
- Bazarr Web UI: http://localhost:6767 or https://bazarr.4eva.me
- Plex Subtitle Format Support: SRT, ASS, SSA, VTT
