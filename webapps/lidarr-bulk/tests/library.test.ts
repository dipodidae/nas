import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ExistingAlbum } from '../server/utils/library'
import type { ParsedItem } from '~~/shared/types'

const libraryArtists = vi.fn()
const libraryAlbums = vi.fn()
vi.mock('../server/utils/lidarr', () => ({
  libraryArtists: () => libraryArtists(),
  libraryAlbums: (id: number) => libraryAlbums(id),
}))

const { clearLibraryCache, findExistingAlbum, matchLibraryAlbum, matchLibraryArtists } = await import('../server/utils/library')

function existing(over: Partial<ExistingAlbum> = {}): ExistingAlbum {
  return { albumId: 1, artistId: 10, title: 'Filosofem', artistName: 'Burzum', percentOfTracks: 100, complete: true, ...over }
}

beforeEach(() => {
  clearLibraryCache()
  libraryArtists.mockReset()
  libraryAlbums.mockReset()
})

describe('matchLibraryArtists', () => {
  const rows = [
    { id: 1, name: 'Burzum' },
    { id: 2, name: 'Баста' },
    { id: 3, name: 'Darkthrone' },
  ]

  it('matches on name', () => {
    expect(matchLibraryArtists('Burzum', rows).map(a => a.id)).toEqual([1])
  })

  it('matches a romanized name against a Cyrillic-tagged library artist', () => {
    expect(matchLibraryArtists('Basta', rows).map(a => a.id)).toEqual([2])
  })

  it('returns nothing for an unknown or empty artist', () => {
    expect(matchLibraryArtists('Nobody At All', rows)).toEqual([])
    expect(matchLibraryArtists('', rows)).toEqual([])
    expect(matchLibraryArtists(undefined, rows)).toEqual([])
  })
})

describe('matchLibraryAlbum', () => {
  const parsed: ParsedItem = { raw: 'x', kind: 'album', artist: 'Burzum', title: 'Filosofem' }

  it('finds the album by exact title', () => {
    const albums = [existing({ albumId: 7, title: 'Filosofem' }), existing({ albumId: 8, title: 'Hliðskjálf' })]
    expect(matchLibraryAlbum(parsed, albums)?.albumId).toBe(7)
  })

  it('does not match a lone unrelated album', () => {
    expect(matchLibraryAlbum(parsed, [existing({ albumId: 9, title: 'Det som engang var' })])).toBeUndefined()
  })

  it('returns undefined for an empty library or a row without a title', () => {
    expect(matchLibraryAlbum(parsed, [])).toBeUndefined()
    expect(matchLibraryAlbum({ raw: 'x', kind: 'album', artist: 'Burzum' }, [existing()])).toBeUndefined()
  })
})

describe('findExistingAlbum', () => {
  it('reports a complete album we already hold', async () => {
    libraryArtists.mockResolvedValue([{ id: 10, name: 'Burzum' }])
    libraryAlbums.mockResolvedValue([existing({ albumId: 5, complete: true, percentOfTracks: 100 })])
    const hit = await findExistingAlbum({ raw: 'x', kind: 'album', artist: 'Burzum', title: 'Filosofem' })
    expect(hit).toMatchObject({ albumId: 5, complete: true })
  })

  it('reports an incomplete album as present but not complete', async () => {
    libraryArtists.mockResolvedValue([{ id: 10, name: 'Burzum' }])
    libraryAlbums.mockResolvedValue([existing({ albumId: 6, complete: false, percentOfTracks: 40 })])
    const hit = await findExistingAlbum({ raw: 'x', kind: 'album', artist: 'Burzum', title: 'Filosofem' })
    expect(hit).toMatchObject({ albumId: 6, complete: false, percentOfTracks: 40 })
  })

  it('returns null when the artist is not in the library', async () => {
    libraryArtists.mockResolvedValue([{ id: 10, name: 'Burzum' }])
    libraryAlbums.mockResolvedValue([])
    expect(await findExistingAlbum({ raw: 'x', kind: 'album', artist: 'Nobody', title: 'Whatever' })).toBeNull()
    expect(libraryAlbums).not.toHaveBeenCalled()
  })

  it('caches the artist index and per-artist albums across rows', async () => {
    libraryArtists.mockResolvedValue([{ id: 10, name: 'Burzum' }])
    libraryAlbums.mockResolvedValue([
      existing({ albumId: 5, title: 'Filosofem' }),
      existing({ albumId: 6, title: 'Hvis lyset tar oss' }),
    ])
    const a = await findExistingAlbum({ raw: 'x', kind: 'album', artist: 'Burzum', title: 'Filosofem' })
    const b = await findExistingAlbum({ raw: 'x', kind: 'album', artist: 'Burzum', title: 'Hvis lyset tar oss' })
    expect(a?.albumId).toBe(5)
    expect(b?.albumId).toBe(6)
    expect(libraryArtists).toHaveBeenCalledTimes(1)
    expect(libraryAlbums).toHaveBeenCalledTimes(1)
  })

  it('degrades to null when the library index is unavailable', async () => {
    libraryArtists.mockRejectedValue(new Error('Lidarr 500'))
    expect(await findExistingAlbum({ raw: 'x', kind: 'album', artist: 'Burzum', title: 'Filosofem' })).toBeNull()
  })

  it('needs both an artist and a title', async () => {
    expect(await findExistingAlbum({ raw: 'x', kind: 'album', title: 'Filosofem' })).toBeNull()
    expect(await findExistingAlbum({ raw: 'x', kind: 'album', artist: 'Burzum' })).toBeNull()
    expect(libraryArtists).not.toHaveBeenCalled()
  })
})
