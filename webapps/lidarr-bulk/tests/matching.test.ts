import { describe, expect, it } from 'vitest'
import type { Candidate, LidarrAlbumCandidate, LidarrArtistCandidate, ParsedItem } from '~~/shared/types'
import { normKey, pickAutoMatch, rankCandidates, similarity } from '../server/utils/matching'

function album(title: string, artist: string, albumType?: string): Candidate {
  return {
    kind: 'album',
    value: { title, albumType, artist: { artistName: artist, foreignArtistId: 'x' } } as unknown as LidarrAlbumCandidate,
  }
}

function artist(name: string): Candidate {
  return {
    kind: 'artist',
    value: { artistName: name, foreignArtistId: 'x' } as unknown as LidarrArtistCandidate,
  }
}

const parsedAlbum: ParsedItem = { raw: 'Iron Maiden - Powerslave', kind: 'album', artist: 'Iron Maiden', title: 'Powerslave' }

describe('pickAutoMatch (album)', () => {
  it('returns the only candidate when there is just one, even without exact match', () => {
    const c = album('Powerslave (Remastered)', 'Iron Maiden')
    expect(pickAutoMatch('album', parsedAlbum, [c])).toBe(c)
  })

  it('finds an exact normalized match anywhere in the list, not just at index 0', () => {
    const compilation = album('Powerslave / Somewhere in Time', 'Iron Maiden')
    const exact = album('powerslave', 'iron maiden') // weird casing — should still match
    const remaster = album('Powerslave (2015 Remaster)', 'Iron Maiden')
    expect(pickAutoMatch('album', parsedAlbum, [compilation, exact, remaster])).toBe(exact)
  })

  it('returns undefined when no exact match and fuzzy is ambiguous (two close candidates)', () => {
    const a = album('Powerslave (1984)', 'Iron Maiden')
    const b = album('Powerslave (Remastered)', 'Iron Maiden')
    expect(pickAutoMatch('album', parsedAlbum, [a, b])).toBeUndefined()
  })

  it('falls back to fuzzy when there is a clear winner above the threshold', () => {
    const compilation = album('Greatest Hits Vol 3', 'Iron Maiden')
    const close = album('Powerslave (Remaster)', 'Iron Maiden')
    expect(pickAutoMatch('album', parsedAlbum, [compilation, close])).toBe(close)
  })

  it('rejects fuzzy candidates whose title is below the strict threshold', () => {
    const wrong = album('Number of the Beast', 'Iron Maiden')
    const other = album('Live After Death', 'Iron Maiden')
    expect(pickAutoMatch('album', parsedAlbum, [wrong, other])).toBeUndefined()
  })

  it('rejects fuzzy candidates with the wrong artist even if the title is close', () => {
    const wrongArtist = album('Powerslave', 'Some Cover Band')
    const compilation = album('Greatest Hits Vol 3', 'Iron Maiden')
    expect(pickAutoMatch('album', parsedAlbum, [wrongArtist, compilation])).toBeUndefined()
  })

  it('prefers albumType=Album over Single/EP when both strictly match title+artist', () => {
    const parsed: ParsedItem = { raw: 'Asphyx - Last One on Earth', kind: 'album', artist: 'Asphyx', title: 'Last One on Earth' }
    const single = album('Last One on Earth', 'Asphyx', 'Single')
    const fullAlbum = album('Last One on Earth', 'Asphyx', 'Album')
    expect(pickAutoMatch('album', parsed, [single, fullAlbum])).toBe(fullAlbum)
  })

  it('still returns undefined when multiple Albums strictly match (genuine ambiguity)', () => {
    const parsed: ParsedItem = { raw: 'X - Y', kind: 'album', artist: 'X', title: 'Y' }
    const a = album('Y', 'X', 'Album')
    const b = album('Y', 'X', 'Album')
    expect(pickAutoMatch('album', parsed, [a, b])).toBeUndefined()
  })
})

describe('pickAutoMatch (artist)', () => {
  const parsed: ParsedItem = { raw: 'Iron Maiden', kind: 'artist' }

  it('matches normalized artist name', () => {
    const exact = artist('Iron Maiden')
    const decoy = artist('Iron Maiden Tribute')
    expect(pickAutoMatch('artist', parsed, [decoy, exact])).toBe(exact)
  })

  it('returns undefined when no candidate matches exactly and fuzzy is ambiguous', () => {
    const a = artist('Iron Maiden Tribute')
    const b = artist('Iron Maidens')
    expect(pickAutoMatch('artist', parsed, [a, b])).toBeUndefined()
  })
})

describe('similarity', () => {
  it('returns 1 for identical strings and 0 for an empty string', () => {
    expect(similarity('powerslave', 'powerslave')).toBe(1)
    expect(similarity('', 'powerslave')).toBe(0)
  })

  it('returns a value between 0 and 1 for close strings', () => {
    const s = similarity('powerslave', 'powerslav')
    expect(s).toBeGreaterThanOrEqual(0.9)
    expect(s).toBeLessThan(1)
  })
})

describe('normKey', () => {
  it('decomposes accents and strips diacritics (ö → o)', () => {
    expect(normKey('Mörder Machine')).toBe(normKey('Morder Machine'))
    expect(normKey('Beyoncé')).toBe('beyonce')
  })

  it('treats horizontal ellipsis and three dots as equivalent', () => {
    expect(normKey('Comprendido!… Time Stop!… …and World Ending'))
      .toBe(normKey('¡Comprendido!... Time Stop! ...and World Ending'))
  })

  it('treats em/en/minus dashes as the same separator', () => {
    expect(normKey('Iron Maiden — Powerslave')).toBe(normKey('Iron Maiden - Powerslave'))
    expect(normKey('Iron Maiden – Powerslave')).toBe(normKey('Iron Maiden - Powerslave'))
  })

  it('matches the auto-match flow on real Unicode-divergent input', () => {
    const parsed: ParsedItem = { raw: 'Deutsch Nepal - Comprendido!… Time Stop!… …and World Ending', kind: 'album', artist: 'Deutsch Nepal', title: 'Comprendido!… Time Stop!… …and World Ending' }
    const cand: Candidate = {
      kind: 'album',
      value: { title: '¡Comprendido!... Time Stop! ...and World Ending', artist: { artistName: 'Deutsch Nepal', foreignArtistId: 'x' } } as unknown as LidarrAlbumCandidate,
    }
    const decoy: Candidate = {
      kind: 'album',
      value: { title: 'A Silent Siege', artist: { artistName: 'Deutsch Nepal', foreignArtistId: 'x' } } as unknown as LidarrAlbumCandidate,
    }
    expect(pickAutoMatch('album', parsed, [decoy, cand])).toBe(cand)
  })
})

describe('rankCandidates', () => {
  it('puts the closest title match first', () => {
    const parsed: ParsedItem = { raw: 'Iron Maiden - Powerslave', kind: 'album', artist: 'Iron Maiden', title: 'Powerslave' }
    const farAway = album('A Matter of Life and Death', 'Iron Maiden')
    const close = album('Powerslave (Remaster)', 'Iron Maiden')
    const exact = album('Powerslave', 'Iron Maiden')
    const ranked = rankCandidates('album', parsed, [farAway, close, exact])
    expect(ranked[0]).toBe(exact)
    expect(ranked[1]).toBe(close)
    expect(ranked[2]).toBe(farAway)
  })

  it('is a no-op when there are fewer than two candidates', () => {
    const parsed: ParsedItem = { raw: 'X', kind: 'album', artist: 'A', title: 'X' }
    const one = album('X', 'A')
    expect(rankCandidates('album', parsed, [one])).toEqual([one])
  })
})
