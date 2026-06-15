import { describe, expect, it } from 'vitest'
import { jellyfinEnabled, pickBestMatch, stripNoise } from '../server/utils/jellyfin'

const ENV = {
  JELLYFIN_URL: 'http://jellyfin:8096',
  JELLYFIN_API_KEY: 'key',
  JELLYFIN_USER_ID: 'uid',
} as never

describe('jellyfinEnabled', () => {
  it('true only when all three vars are set', () => {
    expect(jellyfinEnabled(ENV)).toBe(true)
    expect(jellyfinEnabled({ ...ENV, JELLYFIN_URL: '' } as never)).toBe(false)
    expect(jellyfinEnabled({ ...ENV, JELLYFIN_API_KEY: '' } as never)).toBe(false)
    expect(jellyfinEnabled({ ...ENV, JELLYFIN_USER_ID: '' } as never)).toBe(false)
  })
})

describe('stripNoise', () => {
  it('removes feat./ft. tails, parentheticals, and remaster suffixes', () => {
    expect(stripNoise('Song feat. Other')).toBe('Song')
    expect(stripNoise('Song ft. Other')).toBe('Song')
    expect(stripNoise('Song (feat. Other)')).toBe('Song')
    expect(stripNoise('Dancing - 2010 Remaster')).toBe('Dancing')
    expect(stripNoise('Plain')).toBe('Plain')
  })
})

describe('pickBestMatch', () => {
  const cand = (Id: string, Name: string, Artists: string[]) => ({ Id, Name, Artists, AlbumArtist: Artists[0] })

  it('matches on normalized title + artist', () => {
    const id = pickBestMatch(
      { title: 'A Forest', artist: 'The Cure' },
      [cand('1', 'A Forest', ['The Cure']), cand('2', 'Boys Don’t Cry', ['The Cure'])],
    )
    expect(id).toBe('1')
  })

  it('tolerates feat tags, remaster suffixes, punctuation, and case', () => {
    const id = pickBestMatch(
      { title: 'Dancing (feat. X)', artist: 'Robyn' },
      [cand('9', 'Dancing - 2010 Remaster', ['ROBYN'])],
    )
    expect(id).toBe('9')
  })

  it('returns null when the artist does not match', () => {
    const id = pickBestMatch(
      { title: 'A Forest', artist: 'Joy Division' },
      [cand('1', 'A Forest', ['The Cure'])],
    )
    expect(id).toBeNull()
  })

  it('returns null on an empty candidate list', () => {
    expect(pickBestMatch({ title: 'X', artist: 'Y' }, [])).toBeNull()
  })
})
