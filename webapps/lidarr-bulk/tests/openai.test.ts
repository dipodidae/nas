import { describe, expect, it } from 'vitest'
import { AI_DEFAULT_COUNT, AI_MAX_COUNT, AI_MIN_COUNT, clampCount, normalizeAlbums } from '../server/utils/openai'

describe('clampCount', () => {
  it('defaults when undefined or non-finite', () => {
    expect(clampCount(undefined)).toBe(AI_DEFAULT_COUNT)
    expect(clampCount(Number.NaN)).toBe(AI_DEFAULT_COUNT)
  })
  it('clamps to bounds and truncates', () => {
    expect(clampCount(0)).toBe(AI_MIN_COUNT)
    expect(clampCount(999)).toBe(AI_MAX_COUNT)
    expect(clampCount(12.9)).toBe(12)
  })
})

describe('normalizeAlbums', () => {
  it('shapes rows into Artist - Album ParsedItems', () => {
    expect(normalizeAlbums([{ artist: 'Clan of Xymox', album: 'Medusa' }], 25)).toEqual([
      { raw: 'Clan of Xymox - Medusa', kind: 'album', artist: 'Clan of Xymox', title: 'Medusa' },
    ])
  })
  it('drops rows missing artist or album', () => {
    expect(normalizeAlbums([
      { artist: 'X', album: '' },
      { artist: '', album: 'Y' },
      { album: 'no artist' },
      { artist: 'The Cure', album: 'Faith' },
    ], 25)).toEqual([
      { raw: 'The Cure - Faith', kind: 'album', artist: 'The Cure', title: 'Faith' },
    ])
  })
  it('dedupes case-insensitively and collapses whitespace', () => {
    const out = normalizeAlbums([
      { artist: '  The   Cure ', album: 'Faith' },
      { artist: 'the cure', album: 'FAITH' },
    ], 25)
    expect(out).toEqual([
      { raw: 'The Cure - Faith', kind: 'album', artist: 'The Cure', title: 'Faith' },
    ])
  })
  it('caps at count', () => {
    const raw = Array.from({ length: 10 }, (_, i) => ({ artist: `A${i}`, album: `B${i}` }))
    expect(normalizeAlbums(raw, 3)).toHaveLength(3)
  })
  it('ignores non-string fields', () => {
    expect(normalizeAlbums([{ artist: 42, album: { x: 1 } } as never], 25)).toEqual([])
  })
})
