# ADR-0058 — AudioBookBay feeds bookshelf-audio, through a definition this repo owns

**Date:** 2026-09-27
**Status:** accepted

## Decision

**AudioBookBay** (ABB), the largest public audiobook tracker, is a Prowlarr indexer
built from `prowlarr/definitions/audiobookbay.yml`. The file is tracked here and
mounted read-only, as a **directory**, at `/config/Definitions/Custom/`. Prowlarr
syncs it into `bookshelf-audio` (category 3030), so audiobooks use Bookshelf's own
pipeline: search, grab to qBittorrent (`arr-bookshelf-audio`), import to
`/data/books/audiobooks`, and play in Jellyfin's **Audiobooks** library.

Proven end to end on 2026-09-27: *The Dispossessed* was grabbed from ABB as an M4B,
imported, and plays in Jellyfin (12.5 h).

Prowlarr dropped ABB from its own definitions repo. The base is a community
Cardigann definition whose domains were all dead. `audiobookbay.lu` answers.

## Invariants (each one cost a debugging round)

- **A browser User-Agent on search and download.** ABB answers Prowlarr's own UA
  with a 146-byte `404` on every page. The indexer then looks broken while the site
  is fine.
- **Lowercase, punctuation-free keywords** (`keywordsfilters`). ABB `301`s *any*
  query containing an uppercase letter to its homepage (`?s=Murakami` redirects,
  `?s=murakami` answers). Prowlarr counts that as a failure, and every *arr query is
  Title Cased, so every search failed.
- **`[FORMAT]` appended to the title** (M4B, MP3, ...) from the post body. ABB titles
  name no format, so Bookshelf parsed every release as *Unknown Audio*.
- **English and Dutch rows only**, filtered in the row selector. The *arrs have no
  release-level language filter. The top hit for "Norwegian Wood" was the Spanish
  *Tokio blues* M4B, and Bookshelf approved it.
- **Two pages per query and 60 queries an hour** (Prowlarr query limit on the
  indexer). Every *arr query is a specific "title author", which the first page
  answers. Five pages across a 200-book backlog is how a public site gets an IP
  blocked. During a backlog Prowlarr silently drops the indexer once the cap is
  spent. `check-books-stack.py` knows this and falls back to a direct probe
  before calling ABB broken.
- **qBittorrent appends public trackers to every new torrent**
  (`DEFAULT_TRACKERS` in `qbittorrent_settings_enforce.py`, enforced hourly). ABB
  releases are bare info-hash magnets. The list is the set ABB's own torrents
  announce to.
- **The Spoken profile accepts Unknown Audio as its lowest rank**, below MP3 < FLAC
  < M4B, with M4B as the cutoff. Most general-tracker titles name no format;
  rejecting them all made Knaben useless. Upgrades still move toward M4B.

## The sources that were considered (research, 2026-09-27)

| Source | Verdict |
| --- | --- |
| **AudioBookBay** | Implemented. The biggest public audiobook tracker, no account. |
| **MyAnonamouse** (MAM) | The best source overall: private, curated, rare narrations. Needs an invite application and an IRC interview (Wednesdays and Saturdays). Bookshelf supports MAM natively, without Prowlarr. **Human action: apply at myanonamouse.net/inviteapp.php.** |
| **RuTracker** | Semi-private: free registration, a large English audiobook section, and a Prowlarr definition exists. The best no-invite addition. Needs an account created by a human. |
| Knaben / TPB | Already present. Thin on audiobooks and untagged, which is why Unknown Audio is now accepted. |
| Usenet | Wrong tool here: weak for audio per the community, and it needs a paid provider plus indexer plus SABnzbd. |
| Anna's Archive / Z-Library | Ebooks. Few audiobooks, and AA search is captcha-walled (ADR-0057). |
| audiobook-dl (Storytel, Nextory, BookBeat, Libby/OverDrive, Everand) | Only with a paid subscription or a library card, one book at a time. Not an *arr source. |
| Libation (Audible) | The legal route for audiobooks already bought on Audible: DRM-free M4B of your own library. Not a discovery source. |

## Not done

- **Cleanuparr is not wired to Bookshelf.** A dead ABB torrent (`metaDL`, no seeds;
  *Malafrena* on day one) stays in the queue until someone removes it. Cleanuparr
  supports Readarr-type apps, but it is an armed deletion engine (ADR-0017), and
  adding an app to it deserves its own review rather than a drive-by.
