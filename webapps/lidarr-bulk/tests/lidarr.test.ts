import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { LidarrAlbumCandidate } from '~~/shared/types'
import { addAlbum, monitorAlbums, waitForArtistRefresh } from '../server/utils/lidarr'

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

describe('waitForArtistRefresh', () => {
  beforeEach(() => {
    process.env.LIDARR_URL = 'http://lidarr.test'
    process.env.LIDARR_API_KEY = 'test-key'
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('returns immediately when no RefreshArtist for that artistId is queued or started', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify([
      { name: 'RefreshArtist', status: 'completed', body: { artistId: 7 } },
      { name: 'RefreshArtist', status: 'started', body: { artistId: 99 } },
      { name: 'AlbumSearch', status: 'started', body: { albumIds: [42] } },
    ]), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    const sleep = vi.fn(async () => undefined)
    const r = await waitForArtistRefresh(7, { sleep, timeoutMs: 5_000 })
    expect(r).toEqual({ timedOut: false })
    expect(sleep).not.toHaveBeenCalled()
  })

  it('polls until the matching RefreshArtist leaves queued/started', async () => {
    let n = 0
    const fetchMock = vi.fn(async () => {
      n += 1
      const body = n < 3
        ? [{ name: 'RefreshArtist', status: n === 1 ? 'queued' : 'started', body: { artistId: 7 } }]
        : [{ name: 'RefreshArtist', status: 'completed', body: { artistId: 7 } }]
      return new Response(JSON.stringify(body), { status: 200 })
    })
    vi.stubGlobal('fetch', fetchMock)

    const sleep = vi.fn(async () => undefined)
    const r = await waitForArtistRefresh(7, { sleep, intervalMs: 10, timeoutMs: 5_000 })
    expect(r).toEqual({ timedOut: false })
    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(sleep).toHaveBeenCalledTimes(2)
  })

  it('returns timedOut:true when the deadline expires before refresh drains', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify([
      { name: 'RefreshArtist', status: 'started', body: { artistId: 7 } },
    ]), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    const sleep = vi.fn(async () => undefined)
    const r = await waitForArtistRefresh(7, { sleep, intervalMs: 5, timeoutMs: 30 })
    expect(r).toEqual({ timedOut: true })
  })
})

describe('monitorAlbums', () => {
  beforeEach(() => {
    process.env.LIDARR_URL = 'http://lidarr.test'
    process.env.LIDARR_API_KEY = 'test-key'
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('PUTs /api/v1/album/monitor with the provided ids', async () => {
    const calls: { url: string, method: string, body: unknown }[] = []
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString()
      calls.push({ url, method: init?.method ?? 'GET', body: init?.body ? JSON.parse(init.body as string) : undefined })
      return new Response(null, { status: 204 })
    })
    vi.stubGlobal('fetch', fetchMock)

    await monitorAlbums([1, 2, 3], true)
    expect(calls).toHaveLength(1)
    expect(calls[0].method).toBe('PUT')
    expect(calls[0].url).toBe('http://lidarr.test/api/v1/album/monitor')
    expect(calls[0].body).toEqual({ albumIds: [1, 2, 3], monitored: true })
  })

  it('is a no-op when albumIds is empty', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    await monitorAlbums([], true)
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
