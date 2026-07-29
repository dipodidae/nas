// Album / artist auto-match logic. Pure functions, no I/O.
//
// Lidarr's lookup orders results by an internal MusicBrainz score that doesn't
// reliably put the exact-title match at position 0 — compilations, tribute
// pressings and disambiguated re-releases often outrank the original. Worse, for
// some canonical albums the real release isn't in the returned page at all
// ("Pink Floyd The Wall" returns ten cover/tribute records). So matching here is
// deliberately conservative: it decides in two stages, and when it can't prove
// both the artist and the title it hands the row to the user rather than
// guessing. Retrieval breadth is jobs.ts's problem, not this module's.
//
// Stage 1 — artist gate: a candidate is only considered if its artist is the
//   artist we asked for (exact after cross-script folding, or proven upstream by
//   a MusicBrainz alias hit). This is what stops "Powerslave by Some Cover Band"
//   from winning on title alone.
// Stage 2 — title decision among the gated candidates: an exact comparison-key
//   hit wins outright; otherwise a fuzzy winner must clear a strict threshold
//   *and* beat the runner-up by a margin. Release form (studio album vs
//   compilation/live/combined pressing) breaks ties, never creates them.

import type { Candidate, Kind, ParsedItem } from '~~/shared/types'
import { bestCrossScriptSimilarity, isMixedScript, primaryRomanization } from './script'
// normKey / similarity deliberately live in text.ts and are NOT re-exported from
// here: Nuxt auto-imports every server/utils module, and a re-export makes the
// same symbol resolvable from two paths (which it warns about and resolves
// arbitrarily). Import them from './text'.
import { normKey, similarity } from './text'

const MIN_FUZZY_SIMILARITY = 0.95
const FUZZY_MARGIN = 0.05
// What counts as "this is the same artist". Cross-script folding makes an exact
// romanization hit score a clean 1.0, so this only has to absorb spelling drift
// like "Aleksander Serov" vs MusicBrainz's "Александр Серов" → aleksandr serov.
const ARTIST_IDENTITY = 0.9
// A subtitle-stripped hit ("Золотой век" ≈ "Золотой век: Лучшие песни 1986—1989")
// is good evidence but strictly weaker than a real exact match, so it is scored
// just below 1.0. That keeps a true exact always ahead of it and leaves the
// margin rule able to reject a genuinely ambiguous pair.
const SUBTITLE_DISCOUNT = 0.97
// Unique-clear-winner rule, valid only over a proven artist's complete catalogue
// (see AutoMatchOptions.completeCatalogue). Having enumerated everything the
// artist ever released, one title scoring far above every alternative is strong
// evidence — nothing else they made could plausibly have been meant.
//
// Calibrated against the real cases this exists for, and against the ones it must
// refuse (scores are this module's own, over each artist's full discography):
//   accept  Сплин "25 Кадр"      → "25-й кадр"          0.78 vs 0.31 runner-up
//   refuse  7Б "Я умираю, но…"   → "Я пришёл, чтобы…"   0.42 vs 0.36 — album absent
//   refuse  Никитины "Городок…"  → "Под музыку Вивальди" 0.34 vs 0.34 — song, not album
//   refuse  Кино "Виктор Цой 55" → live bootleg          0.41 vs 0.32 — album absent
const CATALOGUE_MIN_SIMILARITY = 0.7
const CATALOGUE_MIN_MARGIN = 0.25

export function norm(s: string | undefined): string {
  return (s ?? '').toLowerCase().trim().replace(/\s+/g, ' ')
}

// "Powerslave (2015 Remaster)" / "Title [Deluxe Edition]" → strip the qualifier
// before applying normKey, so they can match a bare "Powerslave" / "Title".
export function normKeyLoose(s: string | undefined): string {
  return normKey((s ?? '').replace(/[([][^)\]]*[)\]]/g, ' '))
}

// "Золотой век: Лучшие песни 1986—1989" → "Золотой век". Only fires when a colon
// leaves a substantial head behind, so "Untitled: " or ": Live" aren't mangled
// into nothing. Returns '' when there is no subtitle to strip, so callers can
// tell "no subtitle" from "stripped to the same thing".
export function normKeySubtitle(s: string | undefined): string {
  const raw = s ?? ''
  const idx = raw.search(/\s*[:：]\s*/)
  if (idx <= 0)
    return ''
  const head = raw.slice(0, idx)
  const key = normKeyLoose(head)
  return key.length >= 3 ? key : ''
}

// Every comparison key we're willing to accept for one field, each with the
// confidence ceiling that key implies. Cross-script folding is applied inside
// bestCrossScriptSimilarity, so Cyrillic and romanized forms compare directly.
function keyForms(s: string | undefined, allowSubtitle: boolean): { value: string, ceiling: number }[] {
  const forms = [
    { value: normKey(s), ceiling: 1 },
    { value: normKeyLoose(s), ceiling: 1 },
    { value: normKey(stripEditionAppendix(s ?? '')), ceiling: 1 },
    { value: allowSubtitle ? normKeySubtitle(s) : '', ceiling: SUBTITLE_DISCOUNT },
  ]
  return forms.filter(f => f.value.length > 0)
}

// Best achievable score between two field values across every key form and both
// writing systems. `ceiling` caps weaker key forms so they can't fake a 1.0.
//
// Subtitle stripping is off by default, and that default is load-bearing. Asked
// for "The Wall", Lidarr's search page came back without the real album on it but
// *with* "The Wall: The Film Soundtrack" — which subtitle-strips to an apparent
// near-exact hit and would have been added silently. Absence from a truncated
// ten-row page is not evidence that nothing better exists, so a subtitle-only
// match must never decide there. Inside a complete discography it is evidence,
// which is why pickDiscographyAlbum opts in.
function fieldScore(got: string | undefined, want: string | undefined, allowSubtitle = false): number {
  let best = 0
  for (const g of keyForms(got, allowSubtitle)) {
    for (const w of keyForms(want, allowSubtitle)) {
      const ceiling = Math.min(g.ceiling, w.ceiling)
      const score = Math.min(bestCrossScriptSimilarity(g.value, w.value), ceiling)
      if (score > best)
        best = score
    }
  }
  return best
}

// True exact identity on the primary key, cross-script aware: "Баста" ≡ "Basta",
// "powerslave" ≡ "Powerslave". Deliberately does NOT consider the loose or
// subtitle-stripped forms — those are fuzzy evidence, not identity.
function isKeyExact(got: string | undefined, want: string | undefined): boolean {
  const g = normKey(got)
  const w = normKey(want)
  if (!g || !w)
    return false
  return bestCrossScriptSimilarity(g, w) === 1
}

function albumArtistName(c: Extract<Candidate, { kind: 'album' }>['value']): string | undefined {
  return typeof c.artist === 'string' ? c.artist : c.artist?.artistName
}

// --- Release form -------------------------------------------------------------
// Lidarr happily returns the compilation, the live record, the tribute album and
// the 2-in-1 reissue alongside the studio album the user actually named. These
// penalties are small and subtractive: they order otherwise-equal candidates and
// never promote a candidate past the title threshold.

const SECONDARY_PENALTY: Record<string, number> = {
  compilation: 0.05,
  live: 0.05,
  demo: 0.05,
  remix: 0.05,
  mixtape: 0.04,
  interview: 0.06,
  soundtrack: 0.02,
}
const NON_ALBUM_PENALTY = 0.06
// "Powerslave / Single Collection 2" — two releases pressed together. The user
// asked for one album, so this is almost never what they meant.
const COMBINED_PENALTY = 0.08

// A penalty is waived when the user's own title asked for that form: someone
// typing "Live at Wembley" should not have live albums pushed down.
function wantedForms(wantedTitle: string | undefined): Set<string> {
  const key = normKey(wantedTitle)
  const out = new Set<string>()
  for (const form of Object.keys(SECONDARY_PENALTY)) {
    if (key.includes(form))
      out.add(form)
  }
  return out
}

export function releasePenalty(c: Candidate, wantedTitle?: string): number {
  if (c.kind !== 'album')
    return 0
  const waived = wantedForms(wantedTitle)
  let penalty = 0
  const type = (c.value.albumType ?? '').toLowerCase()
  if (type && type !== 'album')
    penalty += NON_ALBUM_PENALTY
  for (const st of c.value.secondaryTypes ?? []) {
    const key = st.toLowerCase()
    if (!waived.has(key))
      penalty += SECONDARY_PENALTY[key] ?? 0
  }
  // Only a spaced slash means "two releases in one"; "AC/DC" style titles and
  // "Him/Her" are single releases and must not be penalised.
  if (/\s\/\s/.test(c.value.title) && !/\s\/\s/.test(wantedTitle ?? ''))
    penalty += COMBINED_PENALTY
  return penalty
}

// --- Auto-match ---------------------------------------------------------------

export interface AutoMatchOptions {
  // Set when the candidates came from a discography we already proved belongs to
  // the requested artist (a MusicBrainz alias/sortname hit). The artist gate is
  // then satisfied by construction and the decision rests on the title alone.
  artistProven?: boolean
  // Suppresses the "only one candidate, take it" shortcut. That shortcut treats a
  // singleton result as weak evidence, which is right when Lidarr's search chose
  // the pool — one hit for our query means something. It is wrong when *we* chose
  // the pool, as with an artist's discography: an artist having exactly one
  // release says nothing about whether it's the release that was asked for.
  requireTitleEvidence?: boolean
  // Permits a subtitle-stripped title ("Золотой век" ≈ "Золотой век: Лучшие
  // песни 1986—1989") to decide the match. Only safe when the candidate pool is
  // an artist's complete discography, where "no exact title exists" is a real
  // fact rather than an artefact of a truncated search page. See fieldScore.
  allowSubtitleMatch?: boolean
  // Declares the candidate pool to be the artist's *entire* catalogue, which
  // licenses the unique-clear-winner rule below. Same justification as
  // allowSubtitleMatch: within a complete catalogue, the runner-up's score is
  // meaningful information about what else could possibly have been meant.
  completeCatalogue?: boolean
}

interface Scored {
  c: Candidate
  titleScore: number
  titleExact: boolean
  adjusted: number
}

// Rank gated candidates by title score minus release-form penalty. Order is
// stable on ties so callers can detect genuine ambiguity via the margin rule.
function scoreTitles(
  candidates: Candidate[],
  wantTitle: string,
  getTitle: (c: Candidate) => string,
  allowSubtitle: boolean,
): Scored[] {
  return candidates
    .map((c) => {
      const titleScore = fieldScore(getTitle(c), wantTitle, allowSubtitle)
      return {
        c,
        titleScore,
        titleExact: isKeyExact(getTitle(c), wantTitle),
        adjusted: titleScore - releasePenalty(c, wantTitle),
      }
    })
    .sort((a, b) => b.adjusted - a.adjusted)
}

// Pick the single best-formed release among equally-titled candidates, or
// undefined when two are equally well-formed (genuine ambiguity — ask the user).
function bestByReleaseForm(scored: Scored[]): Candidate | undefined {
  if (scored.length === 1)
    return scored[0]!.c
  const sorted = [...scored].sort((a, b) => b.adjusted - a.adjusted)
  const best = sorted[0]!
  const second = sorted[1]!
  return best.adjusted > second.adjusted ? best.c : undefined
}

function pickArtistMatch(parsed: ParsedItem, candidates: Candidate[]): Candidate | undefined {
  const name = (c: Candidate): string => (c.kind === 'artist' ? c.value.artistName : '')
  const want = parsed.raw
  const exact = candidates.filter(c => c.kind === 'artist' && isKeyExact(name(c), want))
  if (exact.length === 1)
    return exact[0]
  if (exact.length > 1)
    return undefined
  const scored = candidates
    .filter(c => c.kind === 'artist')
    .map(c => ({ c, score: fieldScore(name(c), want) }))
    .sort((a, b) => b.score - a.score)
  const best = scored[0]
  if (!best || best.score < MIN_FUZZY_SIMILARITY)
    return undefined
  const second = scored[1]
  if (second && best.score - second.score < FUZZY_MARGIN)
    return undefined
  return best.c
}

export function pickAutoMatch(
  kind: Kind,
  parsed: ParsedItem,
  candidates: Candidate[],
  opts: AutoMatchOptions = {},
): Candidate | undefined {
  if (candidates.length === 0)
    return undefined
  if (candidates.length === 1 && !opts.requireTitleEvidence)
    return candidates[0]

  if (kind === 'artist')
    return pickArtistMatch(parsed, candidates)

  if (!parsed.title || !parsed.artist)
    return undefined
  const albums = candidates.filter(c => c.kind === 'album')
  if (albums.length === 0)
    return undefined

  const getTitle = (c: Candidate): string => (c.kind === 'album' ? c.value.title : '')
  const getArtist = (c: Candidate): string =>
    (c.kind === 'album' ? albumArtistName(c.value) ?? '' : '')

  // Stage 1 — artist gate.
  const gated = opts.artistProven
    ? albums
    : albums.filter(c => fieldScore(getArtist(c), parsed.artist) >= ARTIST_IDENTITY)
  if (gated.length === 0)
    return undefined

  // Stage 2 — title decision. A proven artist lowers the artist bar, never the
  // title bar: "25 Кадр" against Сплин's real "25-й кадр" stays a user decision.
  const scored = scoreTitles(gated, parsed.title, getTitle, opts.allowSubtitleMatch ?? false)
  const exact = scored.filter(s => s.titleExact)
  if (exact.length > 0)
    return bestByReleaseForm(exact)

  const best = scored[0]
  if (!best)
    return undefined
  const second = scored[1]

  if (best.titleScore < MIN_FUZZY_SIMILARITY) {
    // Last chance: a lone standout inside a complete catalogue. Requires both a
    // floor and a wide gap to the runner-up, so "the album simply isn't in this
    // artist's discography" (where everything scores low and close together)
    // still falls through to the user.
    if (
      opts.completeCatalogue
      && best.titleScore >= CATALOGUE_MIN_SIMILARITY
      && (!second || best.adjusted - second.adjusted >= CATALOGUE_MIN_MARGIN)
    ) {
      return best.c
    }
    return undefined
  }
  if (second && best.adjusted - second.adjusted < FUZZY_MARGIN)
    return undefined
  return best.c
}

// Best artist-name score anywhere in the result set, or 1 when there is no artist
// to compare against.
export function bestCandidateArtistScore(parsed: ParsedItem, candidates: Candidate[]): number {
  if (!parsed.artist)
    return 1
  let best = 0
  for (const c of candidates) {
    if (c.kind !== 'album')
      continue
    const score = fieldScore(albumArtistName(c.value) ?? '', parsed.artist)
    if (score > best)
      best = score
  }
  return best
}

// Is any candidate even *plausibly* by the artist that was asked for? This is not
// a match decision — it distinguishes "these results are unrelated noise" from
// "the right artist is here under a name we can't quite confirm", so that only the
// former is reported as not-found instead of rendering a picker full of strangers.
//
// Whole-string similarity cannot make this call. Measured against live results:
//
//   0.714  Татьяна Никитина и Сергей Никитин → "Татьяна и Сергей Никитины"  ← RIGHT
//   0.750  Aleksander Serov                  → "Aleksander Jež"            ← WRONG
//
// The correct artist scores *below* the coincidental one, so no threshold on that
// score can separate them. Word coverage can: Russian declension alters a word
// ending (Никитин→Никитины, 0.875 as a token) and leaves every other word intact,
// whereas a coincidental name shares one word out of two and nothing else. So we
// ask what fraction of the requested name's words appear in the candidate's.
const TOKEN_MATCH = 0.8
const ARTIST_PLAUSIBLE_COVERAGE = 0.75

// Fraction of `wanted`'s words that have a close counterpart in `got`.
function tokenCoverage(wanted: string, got: string): number {
  const w = wanted.split(' ').filter(Boolean)
  const g = got.split(' ').filter(Boolean)
  if (w.length === 0 || g.length === 0)
    return 0
  let matched = 0
  for (const token of w) {
    if (g.some(other => similarity(token, other) >= TOKEN_MATCH))
      matched++
  }
  return matched / w.length
}

// Compared under both tokenizations: raw keys settle same-script pairs, primary
// romanizations settle cross-script ones (Кино ≡ Kino).
function artistPlausibility(wanted: string, got: string): number {
  return Math.max(
    tokenCoverage(normKey(wanted), normKey(got)),
    tokenCoverage(primaryRomanization(wanted), primaryRomanization(got)),
  )
}

export function hasPlausibleArtist(parsed: ParsedItem, candidates: Candidate[]): boolean {
  if (!parsed.artist)
    return true
  const wanted = parsed.artist
  return candidates.some((c) => {
    if (c.kind !== 'album')
      return false
    return artistPlausibility(wanted, albumArtistName(c.value) ?? '') >= ARTIST_PLAUSIBLE_COVERAGE
  })
}

// Candidates whose title is an exact comparison-key hit for the requested album,
// regardless of whether their artist matched. These are the rows worth spending a
// MusicBrainz alias lookup on: the title already proves we found *an* edition of
// the right record, so only the artist's identity is still in question. That is
// the "Мираж" case — romanization can never reach Spotify's "Mirage", but the
// alias list can.
export function albumTitleExactCandidates(parsed: ParsedItem, candidates: Candidate[]): Candidate[] {
  const want = parsed.title ?? parsed.raw
  if (!want)
    return []
  return candidates.filter(c => c.kind === 'album' && isKeyExact(c.value.title, want))
}

// Re-orders Lidarr's lookup result so the closest title+artist match floats to
// the top of the picker. Lidarr's default order follows a MusicBrainz score that
// rarely matches what the user typed, and even when the right release is present
// a compilation or tribute pressing usually outranks it.
export function rankCandidates(kind: Kind, parsed: ParsedItem, candidates: Candidate[]): Candidate[] {
  if (candidates.length < 2)
    return candidates
  // `strict` is a display-ordering tiebreak only: it separates a literal
  // "Powerslave" from "Powerslave (Remaster)", which score identically once the
  // loose key forms are in play. It deliberately never feeds the auto-match
  // margin — there, two paren-variants of one album must stay ambiguous.
  const strictOf = (got: string | undefined, want: string): number =>
    bestCrossScriptSimilarity(normKey(got), normKey(want))

  if (kind === 'artist') {
    return [...candidates]
      .map((c, originalIndex) => ({
        c,
        score: c.kind === 'artist' ? fieldScore(c.value.artistName, parsed.raw) : -1,
        strict: c.kind === 'artist' ? strictOf(c.value.artistName, parsed.raw) : -1,
        originalIndex,
      }))
      .sort((a, b) => (b.score - a.score) || (b.strict - a.strict) || (a.originalIndex - b.originalIndex))
      .map(x => x.c)
  }
  if (!parsed.title && !parsed.artist)
    return candidates
  const wantTitle = parsed.title ?? parsed.raw
  const wantArtist = parsed.artist ?? ''
  return [...candidates]
    .map((c, originalIndex) => {
      if (c.kind !== 'album')
        return { c, score: -1, strict: -1, originalIndex }
      const gotArtist = albumArtistName(c.value) ?? ''
      const title = fieldScore(c.value.title, wantTitle, true)
      const artistScore = wantArtist ? fieldScore(gotArtist, wantArtist) : title
      // Artist identity is weighted a little heavier than title: a right-artist
      // near-title beats a right-title wrong-artist cover band every time.
      const score = (artistScore * 1.2 + title) / 2.2 - releasePenalty(c, wantTitle)
      const strictTitle = strictOf(c.value.title, wantTitle)
      const strict = wantArtist ? (strictOf(gotArtist, wantArtist) + strictTitle) / 2 : strictTitle
      return { c, score, strict, originalIndex }
    })
    .sort((a, b) => (b.score - a.score) || (b.strict - a.strict) || (a.originalIndex - b.originalIndex))
    .map(x => x.c)
}

// "Various Artists" comes in many spellings; Lidarr hides the special VA entity
// from text search, so we detect it up front and resolve such rows by MBID.
const VA_TOKENS = new Set(['various artists', 'various artist', 'various', 'va', 'v a'])
export function isVariousArtists(name: string | undefined): boolean {
  if (!name)
    return false
  const k = normKey(name) // normKey strips punctuation, so "v.a." → "v a"
  return VA_TOKENS.has(k)
}

function stripParens(s: string): string {
  return s.replace(/[([][^)\]]*[)\]]/g, ' ').replace(/\s+/g, ' ').trim()
}

// Trailing "edition" appendices that make a lookup miss when present. Anchored to
// the end so an album literally named "Remaster" isn't mangled mid-title.
const EDITION_SUFFIX = /\s*[-–—]?\s*(?:deluxe|expanded|special|extended|anniversary|legacy|collector'?s|bonus[\s-]?track|remaster(?:ed)?)(?:\s+(?:edition|version|reissue))?\s*$/i

export function stripEditionAppendix(title: string): string {
  const noParens = stripParens(title)
  const noEdition = noParens.replace(EDITION_SUFFIX, '').trim()
  return noEdition || noParens || title
}

// Ordered, deduped Lidarr lookup terms for an album row: as-typed first, then
// progressively stripped variants. searchCandidates tries them until one yields
// a title-similar hit, so a stray "(Deluxe Edition)" no longer dead-ends.
//
// Mixed-script rows are the exception and lead with the bare title. A combined
// "Basta На заре" term is actively poison — the search drops the Cyrillic half
// and matches the Latin fragment against unrelated Swedish "Bästa" records —
// whereas "На заре" alone retrieves the real release. Putting the title first
// means the poison term is usually never spent at all.
// A line carrying more than one dash separator may have been split in the wrong
// place: "A - B - C" yields artist "A" / title "B - C", but "A - B" / "C" is just
// as plausible and nothing in the text says which. Rather than guess a second
// time, we let the search try the whole line — MusicBrainz's fuzzy match over the
// combined string usually finds the release wherever the split landed. Costs
// nothing on rows that match earlier, since the caller stops at the first hit.
const DASH_SEPARATOR = /\s+[–—―-]\s+/g

function isAmbiguouslySplit(raw: string): boolean {
  return (raw.match(DASH_SEPARATOR) ?? []).length >= 2
}

export function albumQueryVariations(parsed: ParsedItem): string[] {
  const title = parsed.title ?? parsed.raw
  const artist = parsed.artist ?? ''
  const term = (t: string): string => (artist ? `${artist} ${t}` : t).trim()
  const parenStripped = stripParens(title)
  const editionStripped = stripEditionAppendix(title)
  const combined = [term(title), term(parenStripped), term(editionStripped)]
  const terms = isMixedScript(artist, title)
    ? [title, ...combined, parenStripped]
    : combined
  if (isAmbiguouslySplit(parsed.raw))
    terms.push(parsed.raw)
  return [...new Set(terms.filter(Boolean))]
}
