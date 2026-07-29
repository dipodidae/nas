import { beforeEach, describe, expect, it, vi } from 'vitest'
import { albumItemsFromTracks, buildAuthorizeUrl, needsRefresh, playlistsFromSearch, resetSpotifyBackoff, spotifyEnabled, spotifyFetch, trackDetailsFromItems } from '../server/utils/spotify'

const ENV = {
  SPOTIFY_API_CLIENT_ID: 'cid',
  SPOTIFY_API_CLIENT_SECRET: 'secret',
  SPOTIFY_REDIRECT_URI: 'https://app.example/api/spotify/callback',
} as never

describe('spotifyEnabled', () => {
  it('true only when all three vars are set', () => {
    expect(spotifyEnabled(ENV)).toBe(true)
    expect(spotifyEnabled({ ...ENV, SPOTIFY_API_CLIENT_SECRET: '' } as never)).toBe(false)
    expect(spotifyEnabled({ ...ENV, SPOTIFY_REDIRECT_URI: '' } as never)).toBe(false)
  })
})

describe('buildAuthorizeUrl', () => {
  it('encodes client_id, redirect_uri, scope and state', () => {
    const url = new URL(buildAuthorizeUrl(ENV, 'xyz'))
    expect(url.origin + url.pathname).toBe('https://accounts.spotify.com/authorize')
    expect(url.searchParams.get('client_id')).toBe('cid')
    expect(url.searchParams.get('response_type')).toBe('code')
    expect(url.searchParams.get('redirect_uri')).toBe('https://app.example/api/spotify/callback')
    expect(url.searchParams.get('state')).toBe('xyz')
    expect(url.searchParams.get('scope')).toContain('playlist-read-private')
    expect(url.searchParams.get('show_dialog')).toBe('true')
  })
})

describe('needsRefresh', () => {
  it('true within the 60s skew window, false outside', () => {
    // expires_at 1_000_000 with a 60_000ms skew → refresh threshold is 940_000.
    expect(needsRefresh({ access_token: 'a', refresh_token: 'r', expires_at: 1_000_000 }, 900_000)).toBe(false)
    expect(needsRefresh({ access_token: 'a', refresh_token: 'r', expires_at: 1_000_000 }, 940_000)).toBe(true)
    expect(needsRefresh({ access_token: 'a', refresh_token: 'r', expires_at: 1_000_000 }, 999_000)).toBe(true)
  })
})

describe('albumItemsFromTracks', () => {
  const track = (id: string, name: string, artist: string) => ({
    is_local: false,
    track: { type: 'track', album: { id, name, artists: [{ name: artist }] } },
  })

  it('dedupes by album id and shapes ParsedItems', () => {
    const res = albumItemsFromTracks([
      track('a1', 'Faith', 'The Cure'),
      track('a1', 'Faith', 'The Cure'), // same album, different track
      track('a2', 'Medusa', 'Clan of Xymox'),
    ])
    expect(res.items).toEqual([
      { raw: 'The Cure - Faith', kind: 'album', artist: 'The Cure', title: 'Faith' },
      { raw: 'Clan of Xymox - Medusa', kind: 'album', artist: 'Clan of Xymox', title: 'Medusa' },
    ])
    expect(res.stats).toEqual({ tracks: 3, skipped: 0, uniqueAlbums: 2 })
  })

  it('skips local files, episodes, and missing album/artist; collapses whitespace', () => {
    const res = albumItemsFromTracks([
      { is_local: true, track: { type: 'track', album: { id: 'x', name: 'Local', artists: [{ name: 'Me' }] } } },
      { is_local: false, track: { type: 'episode', album: { id: 'e', name: 'Pod', artists: [{ name: 'Host' }] } } },
      { is_local: false, track: null },
      { is_local: false, track: { type: 'track', album: { id: 'n', name: '', artists: [{ name: 'A' }] } } },
      { is_local: false, track: { type: 'track', album: { id: 'm', name: 'Disint  egration', artists: [{ name: '  The   Cure ' }] } } },
    ])
    expect(res.items).toEqual([
      { raw: 'The Cure - Disint egration', kind: 'album', artist: 'The Cure', title: 'Disint egration' },
    ])
    expect(res.stats).toEqual({ tracks: 5, skipped: 4, uniqueAlbums: 1 })
  })

  it('returns empty result for an empty playlist', () => {
    expect(albumItemsFromTracks([])).toEqual({ items: [], stats: { tracks: 0, skipped: 0, uniqueAlbums: 0 } })
  })

  it('flags Various Artists comps and carries the release year', () => {
    const res = albumItemsFromTracks([
      { track: { type: 'track', album: { id: 'a1', name: 'Pulp Fiction', album_type: 'compilation', release_date: '1994-09-26', artists: [{ name: 'Various Artists' }] } } },
      { track: { type: 'track', album: { id: 'a2', name: 'Mix', album_type: 'compilation', release_date: '2001', artists: [{ name: 'DJ Real' }] } } },
      { track: { type: 'track', album: { id: 'a3', name: 'Solo LP', album_type: 'album', release_date: '2010-01-01', artists: [{ name: 'A Band' }] } } },
    ] as never)
    expect(res.items[0]).toMatchObject({ kind: 'album', artist: 'Various Artists', title: 'Pulp Fiction', variousArtists: true, year: 1994 })
    // album_type 'compilation' alone flags it even when the credited artist isn't literally "Various Artists"
    expect(res.items[1]).toMatchObject({ variousArtists: true, year: 2001 })
    // a normal album is not flagged and has no variousArtists key
    expect(res.items[2].variousArtists).toBeUndefined()
    expect(res.items[2].year).toBe(2010)
  })
})

describe('playlistsFromSearch', () => {
  const raw = (id: string, name: string) => ({
    id,
    name,
    owner: { display_name: `${name} owner` },
    images: [{ url: `https://img/${id}.jpg` }],
    tracks: { total: 7 },
  })

  it('drops null and id-less entries, maps the rest via trimPlaylist', () => {
    const out = playlistsFromSearch([
      raw('p1', 'Synthwave'),
      null, // Spotify-owned / deprecated playlists come back as null
      { name: 'No id', tracks: { total: 3 } } as never,
      raw('p2', 'Darkwave'),
    ])
    expect(out).toEqual([
      { id: 'p1', name: 'Synthwave', trackCount: 7, imageUrl: 'https://img/p1.jpg', owner: 'Synthwave owner' },
      { id: 'p2', name: 'Darkwave', trackCount: 7, imageUrl: 'https://img/p2.jpg', owner: 'Darkwave owner' },
    ])
  })

  it('returns an empty array for an empty page', () => {
    expect(playlistsFromSearch([])).toEqual([])
  })
})

describe('trackDetailsFromItems', () => {
  const t = (name: string, artist: string, album = 'Some Album', extra = {}) => ({
    is_local: false,
    track: { type: 'track', name, artists: [{ name: artist }], album: { name: album }, ...extra },
  })

  it('maps name + primary artist + album, preserving order and duplicates', () => {
    const out = trackDetailsFromItems([
      t('A Forest', 'The Cure'),
      t('A Forest', 'The Cure'), // duplicate kept — dedupe happens later by Jellyfin id
      t('Stranger', 'Clan of Xymox', 'Medusa'),
    ])
    expect(out).toEqual([
      { title: 'A Forest', artist: 'The Cure', album: 'Some Album' },
      { title: 'A Forest', artist: 'The Cure', album: 'Some Album' },
      { title: 'Stranger', artist: 'Clan of Xymox', album: 'Medusa' },
    ])
  })

  it('skips local files, episodes, and rows missing a title or artist', () => {
    const out = trackDetailsFromItems([
      { is_local: true, track: { type: 'track', name: 'Local', artists: [{ name: 'X' }], album: { name: 'Y' } } },
      { is_local: false, track: { type: 'episode', name: 'Pod', artists: [{ name: 'X' }], album: { name: 'Y' } } },
      { is_local: false, track: { type: 'track', name: '', artists: [{ name: 'X' }], album: { name: 'Y' } } },
      { is_local: false, track: { type: 'track', name: 'NoArtist', artists: [], album: { name: 'Y' } } },
      t('Keep', 'Keeper'),
    ])
    expect(out).toEqual([{ title: 'Keep', artist: 'Keeper', album: 'Some Album' }])
  })
})

describe('spotifyFetch — 429 handling', () => {
  beforeEach(() => {
    resetSpotifyBackoff()
    vi.unstubAllGlobals()
  })

  it('retries after a 429 and honours Retry-After', async () => {
    let calls = 0
    vi.stubGlobal('fetch', vi.fn(async () => {
      calls++
      return calls === 1
        ? { status: 429, ok: false, headers: new Headers({ 'retry-after': '1' }), text: async () => 'slow down' }
        : { status: 200, ok: true, headers: new Headers(), json: async () => ({ items: [] }) }
    }))
    const res = await spotifyFetch('https://api.spotify.com/v1/me/playlists', 'tok')
    expect(res.status).toBe(200)
    expect(calls).toBe(2)
  })

  it('gives up after the attempt cap and returns the 429 rather than hanging', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      status: 429,
      ok: false,
      // 0s so the test doesn't actually wait; the cap is what's under test.
      headers: new Headers({ 'retry-after': '0' }),
      text: async () => 'nope',
    })))
    const res = await spotifyFetch('https://api.spotify.com/v1/me/playlists', 'tok')
    expect(res.status).toBe(429)
    expect(vi.mocked(globalThis.fetch)).toHaveBeenCalledTimes(4)
  })

  it('passes non-429 responses straight through without retrying', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      status: 401,
      ok: false,
      headers: new Headers(),
      text: async () => 'expired',
    })))
    const res = await spotifyFetch('https://api.spotify.com/v1/me', 'tok')
    expect(res.status).toBe(401)
    expect(vi.mocked(globalThis.fetch)).toHaveBeenCalledTimes(1)
  })
})
