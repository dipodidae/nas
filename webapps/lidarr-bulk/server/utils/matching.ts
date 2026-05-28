// Album / artist auto-match logic. Pure functions, no I/O.
//
// Lidarr's lookup orders results by an internal MusicBrainz score that doesn't
// reliably put the exact-title match at position 0 — compilations and
// disambiguated re-releases often outrank the original. We do two passes:
//   1. exact normalized match anywhere in the candidate list, or
//   2. a fuzzy fallback that requires a clear winner above a strict threshold.

import type { Candidate, Kind, ParsedItem } from '~~/shared/types'

const MIN_FUZZY_SIMILARITY = 0.95
const FUZZY_MARGIN = 0.05

export function norm(s: string | undefined): string {
  return (s ?? '').toLowerCase().trim().replace(/\s+/g, ' ')
}

// Aggressive comparison key. NFKD-decomposes Unicode (ö → o + combining ¨),
// strips the combining marks, normalizes smart quotes / em-dashes / ellipsis
// to ASCII equivalents, then strips everything that isn't a letter, digit, or
// space. So "¡Comprendido!… …World Ending" and "Comprendido! ...World Ending"
// both reduce to "comprendido world ending".
export function normKey(s: string | undefined): string {
  return (s ?? '')
    .normalize('NFKD')
    .toLowerCase()
    .replace(/\p{M}/gu, '')
    .replace(/[‘’‚‛′]/g, '\'')
    .replace(/[“”„‟″]/g, '"')
    .replace(/[–—−]/g, '-')
    .replace(/…/g, '...')
    .replace(/[^\p{L}\p{N}\s]/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

// "Powerslave (2015 Remaster)" / "Title [Deluxe Edition]" → strip the qualifier
// before applying normKey, so they can match a bare "Powerslave" / "Title".
export function normKeyLoose(s: string | undefined): string {
  return normKey((s ?? '').replace(/[([][^)\]]*[)\]]/g, ' '))
}

function fieldScore(got: string, want: string): number {
  return Math.max(
    similarity(normKey(got), normKey(want)),
    similarity(normKeyLoose(got), normKeyLoose(want)),
  )
}

function levenshtein(a: string, b: string): number {
  if (a === b)
    return 0
  if (!a.length)
    return b.length
  if (!b.length)
    return a.length
  let prev = Array.from({ length: b.length + 1 }, (_, i) => i)
  for (let i = 1; i <= a.length; i++) {
    const curr: number[] = [i]
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1
      curr.push(Math.min(prev[j]! + 1, curr[j - 1]! + 1, prev[j - 1]! + cost))
    }
    prev = curr
  }
  return prev[b.length]!
}

export function similarity(a: string, b: string): number {
  if (a === b)
    return 1
  if (!a.length || !b.length)
    return 0
  return 1 - levenshtein(a, b) / Math.max(a.length, b.length)
}

function albumArtistName(c: Extract<Candidate, { kind: 'album' }>['value']): string | undefined {
  return typeof c.artist === 'string' ? c.artist : c.artist?.artistName
}

export function pickAutoMatch(
  kind: Kind,
  parsed: ParsedItem,
  candidates: Candidate[],
): Candidate | undefined {
  if (candidates.length === 0)
    return undefined
  if (candidates.length === 1)
    return candidates[0]

  let extract: (c: Candidate) => string[] | null
  let targets: string[]
  if (kind === 'artist') {
    extract = c => c.kind === 'artist' ? [c.value.artistName] : null
    targets = [parsed.raw]
  }
  else {
    if (!parsed.title || !parsed.artist)
      return undefined
    extract = c => c.kind === 'album' ? [c.value.title, albumArtistName(c.value) ?? ''] : null
    targets = [parsed.title, parsed.artist]
  }

  // Pass 1: strict comparison-key match on all fields. One hit auto-picks;
  // multiple hits is ambiguous unless one is an Album and the rest are
  // Single/EP/Other releases of the same name — then we pick the Album.
  const strict = candidates.filter((c) => {
    const got = extract(c)
    return got !== null && got.every((g, i) => normKey(g) === normKey(targets[i] ?? ''))
  })
  if (strict.length === 1)
    return strict[0]
  if (strict.length > 1) {
    if (kind === 'album') {
      const albums = strict.filter(c => c.kind === 'album' && (c.value.albumType ?? '').toLowerCase() === 'album')
      if (albums.length === 1)
        return albums[0]
    }
    return undefined
  }

  // Pass 2: loose + fuzzy. fieldScore is best-of-{strict, parens-stripped}; we
  // require all fields >= MIN_FUZZY_SIMILARITY and a clear margin over runner-up.
  const scored = candidates
    .map((c) => {
      const got = extract(c)
      if (!got)
        return null
      const sims = got.map((g, i) => fieldScore(g, targets[i] ?? ''))
      return { c, sims, minSim: Math.min(...sims), avg: sims.reduce((a, b) => a + b, 0) / sims.length }
    })
    .filter((x): x is { c: Candidate, sims: number[], minSim: number, avg: number } => x !== null)
    .sort((a, b) => b.avg - a.avg)

  const best = scored[0]
  if (!best || best.minSim < MIN_FUZZY_SIMILARITY)
    return undefined
  const second = scored[1]
  if (second && best.avg - second.avg < FUZZY_MARGIN)
    return undefined
  return best.c
}

// Re-orders Lidarr's lookup result so the closest title+artist match floats to
// the top of the picker. Lidarr's default order follows a MusicBrainz score
// that rarely matches what the user typed.
export function rankCandidates(kind: Kind, parsed: ParsedItem, candidates: Candidate[]): Candidate[] {
  if (candidates.length < 2)
    return candidates
  let extract: (c: Candidate) => string[] | null
  let targets: string[]
  if (kind === 'artist') {
    extract = c => c.kind === 'artist' ? [c.value.artistName] : null
    targets = [parsed.raw]
  }
  else {
    if (!parsed.title && !parsed.artist)
      return candidates
    extract = c => c.kind === 'album' ? [c.value.title, albumArtistName(c.value) ?? ''] : null
    targets = [parsed.title ?? parsed.raw, parsed.artist ?? '']
  }
  return [...candidates]
    .map((c, originalIndex) => {
      const got = extract(c)
      if (!got)
        return { c, strict: -1, loose: -1, originalIndex }
      const strict = got.map((g, i) => similarity(normKey(g), normKey(targets[i] ?? '')))
      const loose = got.map((g, i) => similarity(normKeyLoose(g), normKeyLoose(targets[i] ?? '')))
      const avg = (xs: number[]) => xs.reduce((a, b) => a + b, 0) / xs.length
      return { c, strict: avg(strict), loose: avg(loose), originalIndex }
    })
    .sort((a, b) => (b.strict - a.strict) || (b.loose - a.loose) || (a.originalIndex - b.originalIndex))
    .map(x => x.c)
}
