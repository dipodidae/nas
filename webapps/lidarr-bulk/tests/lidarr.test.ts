import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { LidarrAlbumCandidate } from '~~/shared/types'
import { addAlbum } from '../server/utils/lidarr'

const opts = {
  rootFolderPath: '/music',
  qualityProfileId: 1,
  metadataProfileId: 1,
  monitorMode: 'all' as const,
  searchOnAdd: true,
}

const candidate: LidarrAlbumCandidate = {
  foreignAlbumId: 'fid-album-1',
  title: 'Test Album',
  artist: { foreignArtistId: 'fid-artist-1', artistName: 'Test Artist' },
} as unknown as LidarrAlbumCandidate

describe('addAlbum', () => {
  beforeEach(() => {
    process.env.LIDARR_URL = 'http://lidarr.test'
    process.env.LIDARR_API_KEY = 'test-key'
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('after POST /album, PUTs artist/editor + album/monitor so the album lands monitored', async () => {
    const calls: { url: string, method: string, body: unknown }[] = []
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString()
      const method = init?.method ?? 'GET'
      const body = init?.body ? JSON.parse(init.body as string) : undefined
      calls.push({ url, method, body })
      if (url.endsWith('/api/v1/album') && method === 'POST') {
        return new Response(JSON.stringify({ id: 42, artistId: 7 }), { status: 201 })
      }
      return new Response(JSON.stringify([]), { status: 202 })
    })
    vi.stubGlobal('fetch', fetchMock)

    const r = await addAlbum(candidate, opts)
    expect(r).toEqual({ id: 42, artistId: 7 })

    const methods = calls.map(c => `${c.method} ${c.url.replace('http://lidarr.test', '')}`)
    expect(methods).toEqual([
      'POST /api/v1/album',
      'PUT /api/v1/artist/editor',
      'PUT /api/v1/album/monitor',
    ])
    expect(calls[1].body).toEqual({ artistIds: [7], monitored: true })
    expect(calls[2].body).toEqual({ albumIds: [42], monitored: true })
  })
})
