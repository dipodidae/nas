// Alias-verified artist identity and discography lookup, via Lidarr's metadata
// backend (api.lidarr.audio). This is the authoritative fallback for rows the
// fuzzy text search can't settle, and it fixes two independent failure modes:
//
//  1. Romanization can't reach a stage name. "Сплин" transliterates to "splin",
//     but Spotify calls the band "Splean"; "Мираж" transliterates to "mirazh"
//     but Spotify says "Mirage". No table reaches those — they are MusicBrainz
//     *aliases*, and the backend hands them over verbatim:
//       Сплин     → sortname "Splean", aliases ["Splin", "Splean", …]
//       Комбинация → sortname "Kombinaciya"
//     An exact hit there is identity proof, not a fuzzy score.
//
//  2. The text search sometimes never returns the album at all. "Pink Floyd The
//     Wall" comes back as ten tribute and cover records with the real release
//     absent from the page entirely, while the artist's discography contains
//     exactly one "The Wall" (Album, no secondary types). Same for a Latin title
//     under a Cyrillic artist ("Smyslovye Gallyutsinatsii 3000" → zero results,
//     yet Смысловые галлюцинации's discography has "3000").
//
// Known limitation: this path needs Lidarr's artist lookup to surface the artist
// at all. For short romanized names — "Basta", "Kino", "Forum", "7B" — Latin
// homographs saturate all ten result slots and the Cyrillic artist never appears.
// Those rows are handled upstream by the native-script title query instead.

import type { Candidate, LidarrAlbumCandidate, ParsedItem } from '~~/shared/types'
import { loadEnv } from './env'
import { lookupArtist } from './lidarr'
import { albumTitleExactCandidates, artistNameVariants, pickAutoMatch } from './matching'
import { bestCrossScriptSimilarity } from './script'
import { normKey } from './text'

export interface ArtistIdentity {
  mbid: string
  name: string
  sortName?: string
  aliases: string[]
}

export interface DiscographyAlbum {
  mbid: string
  title: string
  type?: string
  secondaryTypes?: string[]
}

export interface Discography extends ArtistIdentity {
  albums: DiscographyAlbum[]
}

// How many artist candidates we're willing to pull a full discography for. Each
// is one HTTP call to the metadata backend; the payload can be large (Pink
// Floyd's is 649 release groups / ~160 KB) so this stays tight.
const MAX_DISCOGRAPHY_FETCHES = 3
// Raised ceiling for homonymous names, where identity alone cannot pick the right
// artist and only "which one released this album" can. See resolveAlbumViaArtist.
const MAX_HOMONYM_FETCHES = 6
// Below this cross-script score a candidate isn't worth a discography fetch.
// Loose on purpose: alias-only names like Сплин↔Splean score ~0.67, and proving
// those is the entire point of this module.
const MIN_CANDIDATE_SIMILARITY = 0.5
// What counts as "this identity is the artist that was asked for". Kept equal to
// the matcher's own ARTIST_IDENTITY so there is exactly one notion of artist
// identity in the codebase rather than a stricter one here and a looser one
// there. An exact hit is the common case; the tolerance only absorbs
// transliteration drift such as MusicBrainz's "Anofriev" against Spotify's
// "Anofriyev", which is the same name spelled under a different scheme.
const MIN_IDENTITY = 0.9

// --- Pure helpers -------------------------------------------------------------

// Word multiset of a name, so "Serov, Aleksander" and "Aleksander Serov" compare
// equal. MusicBrainz sortnames are recorded surname-first, which otherwise makes
// the single most useful romanization field unusable for identity checks.
function tokenKey(s: string): string {
  return normKey(s).split(' ').filter(Boolean).sort().join(' ')
}

// Does this MusicBrainz identity claim the name the user gave us? Checks the
// primary name, the sort name and every alias, cross-script folded — so all of
// "Баста" ≡ "Basta" (romanization), "Сплин" ≡ "Splean" (alias) and
// "Serov, Aleksander" ≡ "Aleksander Serov" (sortname word order) prove true.
//
// Every form compared here is a name the artist is recorded under, so treating
// word order as insignificant is safe; it is not a general-purpose fuzzy match.
export function identityProvesName(identity: ArtistIdentity, wanted: string | undefined): boolean {
  if (!normKey(wanted))
    return false
  const forms = [identity.name, identity.sortName, ...identity.aliases]
    .map(normKey)
    .filter(f => f.length > 0)
  // A Spotify qualifier is not part of the artist's identity: MusicBrainz records
  // the band as "Trial" and keeps "(swe)" in a separate disambiguation field, so
  // the bare forms have to be admissible targets or the name can never be proven.
  const wants = artistNameVariants(wanted).map(normKey).filter(w => w.length > 0)
  return forms.some(form => wants.some((want) => {
    const best = Math.max(
      bestCrossScriptSimilarity(form, want),
      bestCrossScriptSimilarity(tokenKey(form), tokenKey(want)),
    )
    return best >= MIN_IDENTITY
  }))
}

// Present a discography entry as a Lidarr-shaped candidate so the decision can
// reuse the same tested matcher the text-search path uses — including the
// release-form penalties that keep a compilation from beating the studio album.
function asCandidate(album: DiscographyAlbum, artistName: string): Candidate {
  return {
    kind: 'album',
    value: {
      foreignAlbumId: album.mbid,
      title: album.title,
      albumType: album.type,
      secondaryTypes: album.secondaryTypes,
      artist: { artistName },
    } as LidarrAlbumCandidate,
  }
}

// Find the album the user asked for inside a discography we've already proven
// belongs to them. artistProven short-circuits the matcher's artist gate, so the
// decision rests on title and release form alone — exactly the semantics chosen
// for the alias-verified path.
export function pickDiscographyAlbum(
  disco: Discography,
  parsed: ParsedItem,
): DiscographyAlbum | undefined {
  if (disco.albums.length === 0)
    return undefined
  const byMbid = new Map(disco.albums.map(a => [a.mbid, a]))
  const candidates = disco.albums.map(a => asCandidate(a, disco.name))
  const chosen = pickAutoMatch('album', parsed, candidates, {
    artistProven: true,
    requireTitleEvidence: true,
    // Safe here and only here: this pool is the artist's entire catalogue, so
    // both "no exact title exists" and "nothing else comes close" are facts
    // rather than artefacts of a truncated search page.
    allowSubtitleMatch: true,
    completeCatalogue: true,
  })
  if (!chosen || chosen.kind !== 'album')
    return undefined
  return byMbid.get(chosen.value.foreignAlbumId)
}

// --- Metadata backend I/O -----------------------------------------------------

interface MetaArtistDoc {
  id?: unknown
  artistname?: unknown
  sortname?: unknown
  artistaliases?: unknown
  Albums?: unknown
}

function str(v: unknown): string | undefined {
  return typeof v === 'string' && v.length > 0 ? v : undefined
}

function strArray(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
}

export function parseArtistDoc(body: unknown): Discography | null {
  const doc = body as MetaArtistDoc | null
  const mbid = str(doc?.id)
  const name = str(doc?.artistname)
  if (!mbid || !name)
    return null
  const albums: DiscographyAlbum[] = []
  for (const raw of Array.isArray(doc?.Albums) ? doc.Albums : []) {
    const a = raw as { Id?: unknown, Title?: unknown, Type?: unknown, SecondaryTypes?: unknown }
    const albumMbid = str(a?.Id)
    const title = str(a?.Title)
    if (!albumMbid || !title)
      continue
    albums.push({
      mbid: albumMbid,
      title,
      type: str(a?.Type),
      secondaryTypes: strArray(a?.SecondaryTypes),
    })
  }
  return {
    mbid,
    name,
    sortName: str(doc?.sortname),
    aliases: strArray(doc?.artistaliases),
    albums,
  }
}

// --- Cache --------------------------------------------------------------------
// Playlists repeat artists heavily — the playlist that prompted this work had
// five separate Kino albums — so caching turns N lookups per artist into one.

const CACHE_TTL_MS = 60 * 60_000
const CACHE_MAX_ENTRIES = 200

interface CacheEntry<T> { value: T, expiresAt: number }

class TtlCache<T> {
  private map = new Map<string, CacheEntry<T>>()

  get(key: string, now: number): T | undefined {
    const hit = this.map.get(key)
    if (!hit)
      return undefined
    if (hit.expiresAt <= now) {
      this.map.delete(key)
      return undefined
    }
    return hit.value
  }

  set(key: string, value: T, now: number): void {
    // Insertion-ordered eviction; cheap and good enough for a single-replica app.
    if (this.map.size >= CACHE_MAX_ENTRIES) {
      const oldest = this.map.keys().next()
      if (!oldest.done)
        this.map.delete(oldest.value)
    }
    this.map.set(key, { value, expiresAt: now + CACHE_TTL_MS })
  }

  clear(): void {
    this.map.clear()
  }
}

const discographyCache = new TtlCache<Discography | null>()
const artistLookupCache = new TtlCache<{ mbid: string, name: string }[]>()
// Identities learned from rows that already resolved. Lidarr's artist lookup is
// unusable for short romanized names — "Kino" returns ten Latin homographs and
// never surfaces Кино, likewise "Basta", "Forum", "7B" — but a *title* search for
// one of that artist's other albums finds them immediately. Playlists are full of
// several albums by the same artist, so one row resolving teaches the rest:
// "Kino — Звезда по имени Солнце" identifies Кино, and "Kino — 45" (whose bare
// numeric title no search can disambiguate) then resolves from the discography.
const learnedArtists = new TtlCache<{ mbid: string, name: string }>()

export function clearArtistResolveCaches(): void {
  discographyCache.clear()
  artistLookupCache.clear()
  learnedArtists.clear()
  inFlightDiscography.clear()
  inFlightArtistLookup.clear()
}

// Record that `inputName` (as the user/Spotify spelled it) refers to this
// MusicBrainz artist. Only called with an identity that already survived the
// matcher, so this never launders a guess into a fact.
export function learnArtistIdentity(
  inputName: string | undefined,
  mbid: string | undefined,
  name: string | undefined,
): void {
  const key = normKey(inputName)
  if (!key || !mbid || !name)
    return
  learnedArtists.set(key, { mbid, name }, Date.now())
}

export function learnedArtistIdentity(inputName: string | undefined): { mbid: string, name: string } | undefined {
  const key = normKey(inputName)
  return key ? learnedArtists.get(key, Date.now()) : undefined
}

// In-flight coalescing. The TTL caches only help *after* a request completes, but
// phase A runs rows concurrently, so N albums by the same artist fire N identical
// requests simultaneously and every one of them misses the cache. Sharing the
// promise turns that back into one request — which matters most for exactly the
// upstream that starts returning 503 when pushed.
const inFlightDiscography = new Map<string, Promise<Discography | null>>()
const inFlightArtistLookup = new Map<string, Promise<{ mbid: string, name: string }[]>>()

function coalesce<T>(map: Map<string, Promise<T>>, key: string, run: () => Promise<T>): Promise<T> {
  const existing = map.get(key)
  if (existing)
    return existing
  const p = run().finally(() => map.delete(key))
  map.set(key, p)
  return p
}

export async function fetchArtistDiscography(mbid: string): Promise<Discography | null> {
  const now = Date.now()
  const cached = discographyCache.get(mbid, now)
  if (cached !== undefined)
    return cached
  return coalesce(inFlightDiscography, mbid, () => fetchArtistDiscographyUncached(mbid))
}

async function fetchArtistDiscographyUncached(mbid: string): Promise<Discography | null> {
  const now = Date.now()
  const base = loadEnv().LIDARR_METADATA_URL.replace(/\/$/, '')
  let result: Discography | null
  try {
    const res = await fetch(`${base}/artist/${encodeURIComponent(mbid)}`, {
      headers: { 'User-Agent': 'lidarr-bulk' },
    })
    result = res.ok ? parseArtistDoc(await res.json()) : null
  }
  catch (err: unknown) {
    // The metadata backend being down must never fail a job — the caller falls
    // back to whatever the text search produced.
    console.error('[artist-resolve] discography fetch failed for', mbid, err instanceof Error ? err.message : String(err))
    result = null
  }
  discographyCache.set(mbid, result, now)
  return result
}

function lookupArtistCached(name: string): Promise<{ mbid: string, name: string }[]> {
  const key = normKey(name)
  const cached = artistLookupCache.get(key, Date.now())
  if (cached !== undefined)
    return Promise.resolve(cached)
  return coalesce(inFlightArtistLookup, key, () => lookupArtistUncached(name, key))
}

async function lookupArtistUncached(name: string, key: string): Promise<{ mbid: string, name: string }[]> {
  const now = Date.now()
  let out: { mbid: string, name: string }[]
  try {
    const res = await lookupArtist(name)
    out = res
      .filter(a => typeof a.foreignArtistId === 'string' && typeof a.artistName === 'string')
      .map(a => ({ mbid: a.foreignArtistId, name: a.artistName }))
  }
  catch (err: unknown) {
    console.error('[artist-resolve] artist lookup failed for', name, err instanceof Error ? err.message : String(err))
    out = []
  }
  artistLookupCache.set(key, out, now)
  return out
}

// --- Public resolution paths --------------------------------------------------

// Candidate-side alias verification. The text search already returned rows whose
// *title* is an exact match; the only open question is whether the artist behind
// one of them is the artist we asked for. Verifying via MusicBrainz aliases
// settles the "Мираж" ≡ "Mirage" class that no romanization can.
//
// Returns a candidate only when exactly one distinct artist verifies — two
// verified artists with the same album title is genuine ambiguity.
export async function resolveByCandidateAlias(
  parsed: ParsedItem,
  candidates: Candidate[],
): Promise<Candidate | undefined> {
  if (!parsed.artist)
    return undefined
  const exact = albumTitleExactCandidates(parsed, candidates)
  if (exact.length === 0)
    return undefined

  // One fetch per distinct artist, capped.
  const seen = new Set<string>()
  const probes: { candidate: Candidate, mbid: string }[] = []
  for (const c of exact) {
    if (c.kind !== 'album')
      continue
    const artist = c.value.artist
    const mbid = typeof artist === 'string' ? undefined : artist?.foreignArtistId
    if (!mbid || seen.has(mbid))
      continue
    seen.add(mbid)
    probes.push({ candidate: c, mbid })
    if (probes.length >= MAX_DISCOGRAPHY_FETCHES)
      break
  }

  const verified: Candidate[] = []
  for (const probe of probes) {
    const disco = await fetchArtistDiscography(probe.mbid)
    if (disco && identityProvesName(disco, parsed.artist))
      verified.push(probe.candidate)
  }
  return verified.length === 1 ? verified[0] : undefined
}

// Artist-first resolution: prove the artist, then find the album inside their
// discography. Used when the text search produced nothing usable — either no
// results at all, or results the matcher refused to commit to.
//
// Returns the album's MusicBrainz id, which the caller turns back into a real
// Lidarr candidate via lookupAlbum('lidarr:<mbid>') so the normal add path is
// unchanged. Returns null when the artist can't be proven or two proven artists
// both plausibly own the title.
export async function resolveAlbumViaArtist(
  parsed: ParsedItem,
): Promise<{ artist: ArtistIdentity, album: DiscographyAlbum } | null> {
  if (!parsed.artist || !parsed.title)
    return null

  // An identity another row already established beats anything the text search
  // can offer, so try it first and skip the lookup entirely on a hit.
  const learned = learnedArtistIdentity(parsed.artist)
  if (learned) {
    const disco = await fetchArtistDiscography(learned.mbid)
    if (disco) {
      const album = pickDiscographyAlbum(disco, parsed)
      if (album)
        return { artist: disco, album }
    }
  }

  // Try the name as given, then bare forms — "Trial (swe)" finds nothing while
  // "Trial" finds the band. Deduped by mbid across variants.
  const byMbid = new Map<string, { mbid: string, name: string }>()
  for (const variant of artistNameVariants(parsed.artist)) {
    for (const c of await lookupArtistCached(variant)) {
      if (!byMbid.has(c.mbid))
        byMbid.set(c.mbid, c)
    }
    if (byMbid.size > 0)
      break
  }
  const candidates = [...byMbid.values()]
  if (candidates.length === 0)
    return null

  const scored = candidates
    .map((c, index) => ({ ...c, score: bestCrossScriptSimilarity(c.name, parsed.artist), index }))
    .filter(c => c.score >= MIN_CANDIDATE_SIMILARITY)
    .sort((a, b) => (b.score - a.score) || (a.index - b.index))

  // Homonyms need a wider net. MusicBrainz holds several distinct bands called
  // "Tribulation" / "Trial" / "Century", and its search does not put the one you
  // meant first — the Swedish Tribulation is nowhere near the top. Since they all
  // score identically on name, the only way to tell them apart is which one
  // actually released the album, so look at more of them. Only reached after the
  // text search has already failed, so the extra fetches are bounded and rare.
  const topName = normKey(scored[0]?.name)
  const homonyms = scored.filter(c => normKey(c.name) === topName).length
  const budget = homonyms > 1 ? MAX_HOMONYM_FETCHES : MAX_DISCOGRAPHY_FETCHES
  const ranked = scored.slice(0, budget)

  const hits: { artist: ArtistIdentity, album: DiscographyAlbum }[] = []
  for (const c of ranked) {
    const disco = await fetchArtistDiscography(c.mbid)
    if (!disco || !identityProvesName(disco, parsed.artist))
      continue
    const album = pickDiscographyAlbum(disco, parsed)
    if (album)
      hits.push({ artist: disco, album })
  }
  if (hits.length !== 1)
    return null
  return hits[0]!
}
