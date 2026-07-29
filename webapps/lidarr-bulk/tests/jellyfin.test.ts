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

describe('pickBestMatch — cross-script (recreate what we just downloaded)', () => {
  it('matches a romanized Spotify artist to the Cyrillic tags on the file', () => {
    // Exactly the rows that queued and downloaded successfully: Spotify says
    // "Basta", the file Lidarr imported is tagged "Баста".
    const track = { title: 'На заре', artist: 'Basta' }
    const items = [
      { Id: 'wrong', Name: 'На заре', Artists: ['Ольга Рождественская'] },
      { Id: 'right', Name: 'На заре', Artists: ['Баста'] },
    ]
    expect(pickBestMatch(track, items)).toBe('right')
  })

  it('matches a Cyrillic title against a Cyrillic file', () => {
    const track = { title: 'Пыяла', artist: 'AIGEL' }
    expect(pickBestMatch(track, [{ Id: 'a', Name: 'Пыяла', Artists: ['Аигел'] }])).toBe('a')
  })

  it('tolerates a Spotify artist qualifier the file does not carry', () => {
    const track = { title: 'Sulphery', artist: 'Trial (swe)' }
    expect(pickBestMatch(track, [{ Id: 'a', Name: 'Sulphery', Artists: ['Trial'] }])).toBe('a')
  })

  it('still refuses a same-title track by a different artist', () => {
    const track = { title: 'На заре', artist: 'Basta' }
    expect(pickBestMatch(track, [{ Id: 'x', Name: 'На заре', Artists: ['Альянс'] }])).toBeNull()
  })
})
