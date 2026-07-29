import { describe, expect, it } from 'vitest'
import type { Candidate, LidarrAlbumCandidate, LidarrArtistCandidate, ParsedItem } from '~~/shared/types'
import { albumQueryVariations, isVariousArtists, pickAutoMatch, rankCandidates, releasePenalty, stripEditionAppendix } from '../server/utils/matching'
import { normKey, similarity } from '../server/utils/text'

function album(title: string, artist: string, albumType?: string): Candidate {
  return {
    kind: 'album',
    value: { title, albumType, artist: { artistName: artist, foreignArtistId: 'x' } } as unknown as LidarrAlbumCandidate,
  }
}

function albumWith(title: string, artist: string, albumType: string, secondaryTypes: string[]): Candidate {
  return {
    kind: 'album',
    value: { title, albumType, secondaryTypes, artist: { artistName: artist, foreignArtistId: 'x' } } as unknown as LidarrAlbumCandidate,
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

describe('pickAutoMatch — cross-script (real Spotify rows)', () => {
  it('matches a romanized Spotify artist to the Cyrillic MusicBrainz artist', () => {
    // The exact row that produced ten Swedish "Bästa" compilations in the UI.
    const parsed: ParsedItem = { raw: 'Basta - На заре', kind: 'album', artist: 'Basta', title: 'На заре' }
    const right = album('На заре', 'Баста', 'Single')
    const decoys = [
      album('На заре', 'Ольга Рождественская', 'Album'),
      album('На Заре', 'Альянс', 'Album'),
    ]
    // Three different artists released an album with this exact title, so this
    // is genuinely ambiguous on title alone — the artist gate is what decides.
    expect(pickAutoMatch('album', parsed, [...decoys, right])).toBe(right)
  })

  it('matches Аигел / Пыяла from the romanized AIGEL', () => {
    const parsed: ParsedItem = { raw: 'AIGEL - Пыяла', kind: 'album', artist: 'AIGEL', title: 'Пыяла' }
    const right = album('Пыяла', 'Аигел', 'Album')
    const remix = album('Пыяла (Remix)', 'Аигел', 'Single')
    expect(pickAutoMatch('album', parsed, [remix, right])).toBe(right)
  })

  it('still rejects a same-title album by a different Cyrillic artist', () => {
    const parsed: ParsedItem = { raw: 'Kino - Легенда', kind: 'album', artist: 'Kino', title: 'Легенда' }
    const wrongArtist = album('Легенда', 'Ария', 'Album')
    const other = album('45', 'Кино', 'Album')
    expect(pickAutoMatch('album', parsed, [wrongArtist, other])).toBeUndefined()
  })

  it('folds spelling drift between transliteration systems', () => {
    const parsed: ParsedItem = { raw: 'x', kind: 'album', artist: 'Aleksander Serov', title: 'Ты меня любишь' }
    const right = album('Ты меня любишь', 'Александр Серов', 'Album')
    const decoy = album('Ты меня любишь', 'Some Cover Band', 'Album')
    expect(pickAutoMatch('album', parsed, [decoy, right])).toBe(right)
  })
})

describe('pickAutoMatch — artistProven (alias-verified discography)', () => {
  const parsed: ParsedItem = { raw: 'x', kind: 'album', artist: 'Splean', title: '25 Кадр' }

  it('decides on title alone once the artist is proven by a MusicBrainz alias', () => {
    // Сплин's romanization ("splin") never reaches "Splean" — only the alias
    // does — so without artistProven the gate would reject this outright.
    const right = album('25 Кадр', 'Сплин', 'Album')
    const other = album('Гранатовый альбом', 'Сплин', 'Album')
    expect(pickAutoMatch('album', parsed, [other, right], { artistProven: true })).toBe(right)
    expect(pickAutoMatch('album', parsed, [other, right])).toBeUndefined()
  })

  it('still asks when the title is only fuzzy, even with a proven artist', () => {
    // The design decision: alias-proven artist lowers the artist bar, not the
    // title bar. "25 Кадр" vs "25-й кадр" stays a user decision.
    const near = album('25-й кадр и ещё немного', 'Сплин', 'Album')
    const other = album('Реверсивная хроника событий', 'Сплин', 'Album')
    expect(pickAutoMatch('album', parsed, [near, other], { artistProven: true })).toBeUndefined()
  })
})

describe('pickAutoMatch — release form', () => {
  const parsed: ParsedItem = { raw: 'x', kind: 'album', artist: 'Iron Maiden', title: 'Powerslave' }

  it('prefers the studio album over a combined 2-in-1 pressing', () => {
    const combined = album('Powerslave / Single Collection 2', 'Iron Maiden', 'Album')
    const studio = album('Powerslave', 'Iron Maiden', 'Album')
    expect(pickAutoMatch('album', parsed, [combined, studio])).toBe(studio)
  })

  it('prefers the studio album over a same-titled compilation', () => {
    const comp = albumWith('Powerslave', 'Iron Maiden', 'Album', ['Compilation'])
    const studio = album('Powerslave', 'Iron Maiden', 'Album')
    expect(pickAutoMatch('album', parsed, [comp, studio])).toBe(studio)
  })

  it('does not penalise a live release the user actually asked for', () => {
    const liveParsed: ParsedItem = { raw: 'x', kind: 'album', artist: 'Iron Maiden', title: 'Live After Death' }
    const live = albumWith('Live After Death', 'Iron Maiden', 'Album', ['Live'])
    const studio = album('Somewhere in Time', 'Iron Maiden', 'Album')
    expect(pickAutoMatch('album', liveParsed, [studio, live])).toBe(live)
  })

  it('does not treat an unspaced slash in a title as a combined release', () => {
    const acdc: ParsedItem = { raw: 'x', kind: 'album', artist: 'AC/DC', title: 'Back in Black' }
    const right = album('Back in Black', 'AC/DC', 'Album')
    const other = album('Highway to Hell', 'AC/DC', 'Album')
    expect(pickAutoMatch('album', acdc, [other, right])).toBe(right)
  })
})

describe('pickAutoMatch — subtitle tolerance', () => {
  const nautilus: ParsedItem = { raw: 'x', kind: 'album', artist: 'Nautilus Pompilius', title: 'Золотой век' }
  const subtitled = album('Золотой век: Лучшие песни 1986—1989', 'Nautilus Pompilius', 'Album')
  const other = album('Крылья', 'Nautilus Pompilius', 'Album')

  it('does NOT decide on a subtitle-only match from a search page', () => {
    // Load-bearing safety property. Asked for "The Wall", Lidarr's search page
    // came back without the real album but with "The Wall: The Film Soundtrack";
    // deciding on the subtitle form there silently added the wrong release.
    // Absence from a truncated page is not evidence that nothing better exists.
    const pf: ParsedItem = { raw: 'x', kind: 'album', artist: 'Pink Floyd', title: 'The Wall' }
    const soundtrack = album('The Wall: The Film Soundtrack', 'Pink Floyd', 'Album')
    const unrelated = album('Animals', 'Pink Floyd', 'Album')
    expect(pickAutoMatch('album', pf, [soundtrack, unrelated])).toBeUndefined()
    expect(pickAutoMatch('album', nautilus, [other, subtitled])).toBeUndefined()
  })

  it('does decide on a subtitle-only match inside a complete discography', () => {
    // Real row: Spotify says "Золотой век", MusicBrainz says
    // "Золотой век: Лучшие песни 1986—1989". Within the artist's full catalogue
    // "no exact title exists" is a fact, so the subtitle form may decide.
    expect(pickAutoMatch('album', nautilus, [other, subtitled], {
      artistProven: true,
      allowSubtitleMatch: true,
    })).toBe(subtitled)
  })

  it('lets a literal exact title beat a subtitled one', () => {
    const parsed: ParsedItem = { raw: 'x', kind: 'album', artist: 'Radiohead', title: 'OK Computer' }
    const oknotok = albumWith('OK Computer: OKNOTOK 1997 2017', 'Radiohead', 'Album', ['Compilation'])
    const exact = album('OK Computer', 'Radiohead', 'Album')
    expect(pickAutoMatch('album', parsed, [oknotok, exact])).toBe(exact)
    expect(pickAutoMatch('album', parsed, [oknotok, exact], {
      artistProven: true,
      allowSubtitleMatch: true,
    })).toBe(exact)
  })

  it('still ranks a subtitled match near the top of the picker', () => {
    // Ranking may use subtitle evidence freely — it only orders, never decides.
    const ranked = rankCandidates('album', nautilus, [other, subtitled])
    expect(ranked[0]).toBe(subtitled)
  })
})

describe('releasePenalty', () => {
  it('is zero for a clean studio album', () => {
    expect(releasePenalty(album('Powerslave', 'Iron Maiden', 'Album'))).toBe(0)
  })

  it('penalises non-album types, secondary types and combined pressings', () => {
    expect(releasePenalty(album('Powerslave', 'Iron Maiden', 'Single'))).toBeGreaterThan(0)
    expect(releasePenalty(albumWith('Powerslave', 'Iron Maiden', 'Album', ['Live']))).toBeGreaterThan(0)
    expect(releasePenalty(album('Powerslave / Somewhere in Time', 'Iron Maiden', 'Album'))).toBeGreaterThan(0)
  })

  it('never penalises an artist candidate', () => {
    expect(releasePenalty(artist('Iron Maiden'))).toBe(0)
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

describe('isVariousArtists', () => {
  it('matches the various-artists tokens, case/space-insensitive', () => {
    for (const s of ['Various Artists', 'various', 'VA', 'v.a.', '  Various   Artists '])
      expect(isVariousArtists(s)).toBe(true)
  })
  it('does not match real artist names or empty', () => {
    for (const s of ['Variation', 'The Various', 'Avant', '', undefined])
      expect(isVariousArtists(s)).toBe(false)
  })
})

describe('stripEditionAppendix', () => {
  it('strips parenthetical and trailing edition suffixes', () => {
    expect(stripEditionAppendix('Powerslave (2015 Remaster)')).toBe('Powerslave')
    expect(stripEditionAppendix('The Album - Deluxe Edition')).toBe('The Album')
    expect(stripEditionAppendix('Songs (Expanded Edition)')).toBe('Songs')
  })
  it('leaves a clean title unchanged', () => {
    expect(stripEditionAppendix('Master of Puppets')).toBe('Master of Puppets')
  })
})

describe('albumQueryVariations', () => {
  it('orders original, paren-stripped, edition-stripped, deduped, with the artist prefix', () => {
    expect(albumQueryVariations({ raw: 'x', kind: 'album', artist: 'Iron Maiden', title: 'Powerslave (2015 Remaster)' }))
      .toEqual(['Iron Maiden Powerslave (2015 Remaster)', 'Iron Maiden Powerslave'])
  })
  it('a clean title yields a single variation', () => {
    expect(albumQueryVariations({ raw: 'x', kind: 'album', artist: 'A', title: 'B' })).toEqual(['A B'])
  })
})
