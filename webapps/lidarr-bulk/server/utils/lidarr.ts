// Thin Lidarr v1 HTTP client. Endpoint shapes verified against a live
// Lidarr 3.1.2-nightly instance (see ../../README.md "API reference").

import type {
  LidarrAlbumCandidate,
  LidarrArtistCandidate,
  LidarrProfilesResponse,
} from '~~/shared/types'
import { loadEnv } from './env'

interface AddArtistBody {
  foreignArtistId: string
  artistName: string
  qualityProfileId: number
  metadataProfileId: number
  rootFolderPath: string
  monitored: true
  monitorNewItems: 'all' | 'none'
  addOptions: {
    monitor: 'all' | 'future' | 'none'
    searchForMissingAlbums: boolean
  }
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const env = loadEnv()
  const url = `${env.LIDARR_URL.replace(/\/$/, '')}${path}`
  const headers = new Headers(init.headers)
  headers.set('X-Api-Key', env.LIDARR_API_KEY)
  if (init.body && !headers.has('content-type'))
    headers.set('content-type', 'application/json')
  const res = await fetch(url, { ...init, headers })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`Lidarr ${res.status} ${res.statusText} on ${path}: ${text.slice(0, 200)}`)
  }
  if (res.status === 204)
    return undefined as T
  return res.json() as Promise<T>
}

export async function systemStatus(): Promise<{ version: string, appName: string }> {
  return call('/api/v1/system/status')
}

export async function getProfiles(): Promise<LidarrProfilesResponse> {
  const [rootFolders, qualityProfiles, metadataProfiles] = await Promise.all([
    call<LidarrProfilesResponse['rootFolders']>('/api/v1/rootfolder'),
    call<LidarrProfilesResponse['qualityProfiles']>('/api/v1/qualityprofile'),
    call<LidarrProfilesResponse['metadataProfiles']>('/api/v1/metadataprofile'),
  ])
  return { rootFolders, qualityProfiles, metadataProfiles }
}

export function lookupArtist(term: string): Promise<LidarrArtistCandidate[]> {
  return call(`/api/v1/artist/lookup?term=${encodeURIComponent(term)}`)
}

export function lookupAlbum(term: string): Promise<LidarrAlbumCandidate[]> {
  return call(`/api/v1/album/lookup?term=${encodeURIComponent(term)}`)
}

export interface AddArtistOptions {
  rootFolderPath: string
  qualityProfileId: number
  metadataProfileId: number
  monitorMode: 'all' | 'future'
  searchOnAdd: boolean
}

export function addArtist(c: LidarrArtistCandidate, o: AddArtistOptions): Promise<{ id: number }> {
  const body: AddArtistBody = {
    foreignArtistId: c.foreignArtistId,
    artistName: c.artistName,
    qualityProfileId: o.qualityProfileId,
    metadataProfileId: o.metadataProfileId,
    rootFolderPath: o.rootFolderPath,
    monitored: true,
    monitorNewItems: o.monitorMode === 'future' ? 'all' : 'all',
    addOptions: {
      monitor: o.monitorMode,
      searchForMissingAlbums: o.searchOnAdd,
    },
  }
  return call('/api/v1/artist', { method: 'POST', body: JSON.stringify(body) })
}

// Adding a specific album: tell Lidarr to add the album + its artist with
// monitor=none for other albums, then explicitly monitor the just-added album
// + its parent artist. Lidarr applies the artist's addOptions.monitor to the
// album too, so the top-level `monitored: true` on the POST body is ignored —
// we PUT /album/monitor and /artist/editor afterward to force the right state.
// If the artist already exists Lidarr links it (PUT is a no-op in that case).
export interface AddAlbumOptions extends AddArtistOptions {}

export async function addAlbum(c: LidarrAlbumCandidate, o: AddAlbumOptions): Promise<{ id: number, artistId: number }> {
  const artistFid = typeof c.artist === 'string' ? undefined : c.artist?.foreignArtistId
  const artistName = typeof c.artist === 'string' ? c.artist : c.artist?.artistName
  const body = {
    foreignAlbumId: c.foreignAlbumId,
    monitored: true,
    addOptions: { searchForNewAlbum: o.searchOnAdd },
    artist: {
      foreignArtistId: artistFid,
      artistName,
      qualityProfileId: o.qualityProfileId,
      metadataProfileId: o.metadataProfileId,
      rootFolderPath: o.rootFolderPath,
      monitored: true,
      monitorNewItems: 'none',
      addOptions: {
        monitor: 'none',
        searchForMissingAlbums: false,
      },
    },
  }
  const created = await call<{ id: number, artistId: number }>(
    '/api/v1/album',
    { method: 'POST', body: JSON.stringify(body) },
  )
  if (created?.artistId) {
    await call('/api/v1/artist/editor', {
      method: 'PUT',
      body: JSON.stringify({ artistIds: [created.artistId], monitored: true }),
    })
  }
  if (created?.id) {
    await call('/api/v1/album/monitor', {
      method: 'PUT',
      body: JSON.stringify({ albumIds: [created.id], monitored: true }),
    })
  }
  return created
}

export function commandSearchArtist(artistId: number): Promise<unknown> {
  return call('/api/v1/command', {
    method: 'POST',
    body: JSON.stringify({ name: 'ArtistSearch', artistId }),
  })
}

export function commandSearchAlbum(albumIds: number[]): Promise<unknown> {
  return call('/api/v1/command', {
    method: 'POST',
    body: JSON.stringify({ name: 'AlbumSearch', albumIds }),
  })
}

// --- Health cache (60s TTL) used by /healthz so probes don't hammer Lidarr ---
let lastCheck = 0
let lastOk = false
export async function healthCheck(): Promise<boolean> {
  const now = Date.now()
  if (now - lastCheck < 60_000)
    return lastOk
  try {
    await systemStatus()
    lastOk = true
  }
  catch {
    lastOk = false
  }
  lastCheck = now
  return lastOk
}
