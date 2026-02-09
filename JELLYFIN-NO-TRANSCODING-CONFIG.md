# Jellyfin Direct Play Only Configuration

## ⚠️ IMPORTANT: NO TRANSCODING POLICY

This server is configured for **direct play ONLY**. Files must never be transcoded.

## Docker Configuration Changes Applied

### 1. Removed Transcoding Resources

- ❌ Removed transcoding cache volume
- ❌ Removed tmpfs (was for transcode temp files)
- ❌ Removed shm_size (was for transcoding shared memory)

### 2. Reduced Resource Allocation

Since no transcoding = much lower CPU/RAM needs:

- **CPU:** 2.0 cores → 1.0 core
- **Memory:** 3GB → 1.5GB
- **Reservation:** 512MB → 256MB

### 3. Kept Important Settings

- ✅ I/O priority: weight 800 (high)
- ✅ Read-only media mount
- ✅ Network optimizations
- ✅ Health checks

## Required Jellyfin UI Configuration

**To ensure transcoding is completely disabled, configure these settings in Jellyfin:**

### Settings → Playback

1. **Transcoding Thread Count:** 0
2. **Hardware Acceleration:** None
3. **Transcoding Temporary Path:** Leave empty or disable

### Settings → Playback → Video Settings

1. **Max Streaming Bitrate:** Set to "Auto" or very high (120 Mbps)
2. **Internet Streaming Bitrate:** Unlimited
3. **Allow Video Playback Remuxing:** Enabled
4. **Prefer fMP4-HLS Container:** Disabled

### Settings → Playback → Transcoding

1. **Enable Hardware Encoding:** ❌ Disabled
2. **Enable Hardware Decoding:** ❌ Disabled
3. **Enable Tone Mapping:** ❌ Disabled
4. **Allow Encoding in HEVC Format:** ❌ Disabled
5. **Allow Encoding in AV1 Format:** ❌ Disabled

### Settings → Users → [Each User] → Playback

For each user, set:

1. **Enable Video Playback:** ✅ Enabled
2. **Enable Audio Playback:** ✅ Enabled
3. **Enable Media Conversion:** ❌ **DISABLED** ⚠️
4. **Allow Media Downloading:** As desired
5. **Internet Streaming Bitrate Limit:** Unlimited

### Settings → Server → Transcoding

1. **Transcoding Thread Count:** 0
2. **Hardware Acceleration:** None
3. **Delete Segments After:** N/A (won't create any)
4. **Enable Throttling:** Disabled (not needed for direct play)

## Client Compatibility Requirements

Since transcoding is disabled, **all clients must support:**

### Video Codecs:

- H.264 (AVC) - most compatible
- H.265 (HEVC) - 4K content
- VP9 - some web content
- AV1 - future-proof

### Audio Codecs:

- AAC - most common
- AC3 - 5.1 surround
- EAC3 (DD+) - Dolby Digital Plus
- TrueHD / DTS-HD - lossless formats
- Opus / Vorbis - web formats

### Containers:

- MP4 / M4V
- MKV (Matroska)
- WebM
- TS (Transport Stream)

### Client Support:

✅ **Good clients** (native codec support):

- Jellyfin Media Player (desktop)
- Jellyfin for Android TV
- Jellyfin for Apple TV
- Modern web browsers (Chrome, Firefox, Safari)
- VLC-based clients
- Kodi with Jellyfin plugin

⚠️ **May have issues:**

- Older Smart TVs
- Roku (limited codec support)
- Some web browsers on older devices
- iOS Safari (limited container support)

## Media Library Best Practices

To ensure maximum compatibility without transcoding:

### Recommended Encode Settings:

```
Video Codec: H.264 (High Profile, Level 4.1)
Container: MP4 or MKV
Audio: AAC stereo or AC3 5.1
Resolution: 1080p or 4K
Bitrate: As high as needed for quality
```

### For Maximum Compatibility:

- Use MP4 containers (vs MKV)
- Use AAC audio (vs AC3/DTS)
- Use H.264 video (vs HEVC/AV1)
- Avoid exotic codecs (ProRes, FFV1, etc.)

### For Best Quality (with compatible clients):

- Use MKV containers (more flexible)
- Use HEVC for 4K content (better compression)
- Use TrueHD/DTS-HD for lossless audio
- Keep multiple audio tracks

## Network Requirements

Without transcoding, full bitrate streaming requires:

| Quality        | Bitrate     | Min Network Speed |
| -------------- | ----------- | ----------------- |
| 1080p Standard | 5-10 Mbps   | 20 Mbps           |
| 1080p High     | 10-20 Mbps  | 30 Mbps           |
| 4K HDR         | 40-80 Mbps  | 100 Mbps          |
| 4K HDR Remux   | 80-150 Mbps | 200 Mbps          |

**Ensure your network can handle peak bitrates!**

### Local Network:

- Gigabit Ethernet: ✅ Perfect
- WiFi 5 (AC): ✅ Usually good
- WiFi 6 (AX): ✅ Excellent
- WiFi 4 (N): ⚠️ May struggle with 4K

### Remote Streaming:

- Check your upload speed at home
- Remote clients may need lower quality files
- Consider pre-encoded lower quality versions for remote access

## Monitoring Direct Play

Check Jellyfin Dashboard to verify direct play:

1. **Dashboard → Activity**
2. When streaming, should show:
   - ✅ "Direct Play" or "Direct Stream"
   - ❌ Never "Transcoding"

If you see transcoding:

1. Check client codec support
2. Check network bandwidth
3. Check user playback settings
4. Check file compatibility

## Benefits of Direct Play Only

1. **🔥 Zero CPU Load** - No processing, just streaming files
2. **⚡ Instant Start** - No buffering for transcoding
3. **💎 Original Quality** - No quality loss from re-encoding
4. **💾 Less Disk I/O** - No temp files, no cache writes
5. **⚡ Lower Power** - Cooler system, less electricity
6. **🎯 Simpler System** - Fewer moving parts, less to break

## Troubleshooting

### If playback fails:

1. **Check codec support** in client
2. **Try different client** (Jellyfin Media Player always works)
3. **Check file with MediaInfo** to verify codecs
4. **Check network speed** with speedtest
5. **Check Jellyfin logs** for specific errors

### If quality is poor:

- Not a transcoding issue (we don't transcode!)
- Check source file quality
- Check network bandwidth
- Check client display capabilities

## Applied Changes

Run to apply configuration:

```bash
cd ~/nas
docker compose up -d jellyfin
```

Check logs:

```bash
docker logs jellyfin --tail 50
```

## Monitoring Commands

```bash
# Verify Jellyfin is using less resources now
docker stats jellyfin --no-stream

# Should see much lower CPU and memory usage
# Expected: ~200-400MB RAM, <5% CPU when streaming
```

---

**Remember:** With these settings, incompatible files will simply fail to play rather than transcode. This is by design. Ensure your media library is encoded with compatible codecs!
