# Prowlarr Indexers Configuration Guide

## Summary

Your Prowlarr configuration now includes **106 public torrent indexers** with optimized priorities.

## Current Status

- **Currently Enabled**: 21 indexers
- **Configured in YAML**: 106 indexers
- **Successfully Prioritized**: 20/21 (95%)
- **Needs Manual Update**: 1 indexer (Internet Archive)

## Manual Action Required

### Internet Archive Priority Update

The API times out when updating this indexer. Please manually update:

1. Open: http://localhost:9696/settings/indexers
2. Find: **Internet Archive**
3. Set priority to: **18**

## Recommended High-Value Indexers to Enable

These are the top indexers you should add to maximize coverage:

### Tier 1 - Add These First (High Priority)

| Indexer | Priority | Why Enable |
|---------|----------|------------|
| **BitSearch** | 17 | Modern DHT search engine, excellent coverage |
| **Isohunt2** | 19 | Successor to Isohunt, very reliable |
| **ExtraTorrent.st** | 23 | ExtraTorrent clone, good variety |
| **0Magnet** | 26 | Magnet link database, good fallback |
| **Anidex** | 6 | If you watch anime - excellent tracker |
| **Tokyo Toshokan** | 7 | If you watch anime - Japanese content |
| **kickasstorrents.to** | 14 | KAT mirror, still reliable |
| **kickasstorrents.ws** | 14 | KAT mirror alternative |
| **RuTracker.RU** | 24 | Massive Russian library, great coverage |

### Tier 2 - Additional Coverage (Medium Priority)

| Indexer | Priority | Category |
|---------|----------|----------|
| **Magnetz** | 40 | Magnet search engine |
| **Magnet Cat** | 40 | Magnet directory |
| **BTdirectory** | 40 | Torrent directory |
| **Byrutor** | 48 | Russian tracker |
| **sosulki** | 48 | Russian tracker |
| **BitRu** | 48 | Russian tracker |

### Tier 3 - Specialized Content (If Needed)

**Gaming:**
- GamesTorrents (50)
- BlueRoms (50) - ROM files
- SkidrowRepack (50) - Game repacks

**Software:**
- CrackingPatching (50)
- PC-torrent (50) - Already enabled

**Media Production:**
- VST Torrentz (50) - Already enabled
- VSTHouse (50) - Already enabled
- VSTorrent (50) - Already enabled

**eBooks:**
- EBookBay (50)

**Movies:**
- MoviesDVDR (50) - DVD releases

## How to Enable New Indexers in Prowlarr

### Method 1: Via Prowlarr UI

1. Navigate to: http://localhost:9696/settings/indexers
2. Click "Add Indexer" (+ button)
3. Search for the indexer name
4. Click on it and configure (usually no config needed for public trackers)
5. Save

The priority will be automatically applied by the script on next run!

### Method 2: Bulk Enable (Recommended)

After adding indexers manually, run the priority setter:

```bash
cd /home/tom/nas
python3 scripts/prowlarr_priority_setter.py
```

This will automatically assign the correct priorities to all enabled indexers.

## Priority Tiers Explained

### Tier 1: Premium Quality (Priority 1-10)
- **Best reliability and quality**
- Used first for all searches
- Examples: YTS, Nyaa.si, EZTV

### Tier 2: Mid-Quality (Priority 11-30)
- **Good general trackers**
- Balanced quality and coverage
- Examples: 1337x, The Pirate Bay, BitSearch

### Tier 3: Standard (Priority 31-40)
- **Decent trackers and aggregators**
- Used for additional coverage
- Examples: Knaben, TorrentsCSV, metasearch engines

### Tier 4: Backup/Specialized (Priority 41-50)
- **Fallback options and niche content**
- Regional trackers, specialized content
- Examples: Regional trackers, software, adult content

## Maintenance Commands

### Check Current Status
```bash
cd /home/tom/nas
python3 scripts/prowlarr_priority_checker.py
```

### Apply Priority Changes
```bash
cd /home/tom/nas
python3 scripts/prowlarr_priority_setter.py
```

### Preview Changes (Dry Run)
```bash
cd /home/tom/nas
python3 scripts/prowlarr_priority_setter.py --dry-run
```

## Coverage Statistics

With all recommended indexers enabled, you would have:

- **General Trackers**: 15+ sources
- **Anime Trackers**: 10+ specialized sources
- **Music Trackers**: 2+ lossless sources
- **Regional Content**: 20+ country-specific trackers
- **Specialized**: Gaming, software, eBooks, etc.

## Quick Start: Essential 10

If you want to quickly boost coverage, add these 10 indexers first:

1. BitSearch (17)
2. Isohunt2 (19)
3. kickasstorrents.to (14)
4. ExtraTorrent.st (23)
5. RuTracker.RU (24)
6. 0Magnet (26)
7. Anidex (6) - if you watch anime
8. Tokyo Toshokan (7) - if you watch anime
9. Magnetz (40)
10. BTdirectory (40)

## Notes

- All indexers in the config are **public** and require no authentication
- Priorities can be adjusted in `prowlarr-config.yml`
- Run the setter script after any configuration changes
- Some indexers may be blocked in certain regions - this is normal
- Adult content indexers are set to priority 50 (lowest)

## Configuration File Location

`/home/tom/nas/scripts/prowlarr-config.yml`

Last updated: 2026-02-18
