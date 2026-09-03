import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { JobSnapshot, ParsedItem } from '~~/shared/types'

// Tracks overlap of addAlbum calls so we can assert the scheduling invariant:
// concurrent across artists, strictly serial within one artist.
const active = new Map<string, number>()
let peakTotal = 0
let sameArtistOverlap = 0
const addOrder: string[] = []

interface StubAlbum {
  foreignAlbumId: string
  title: string
  albumType: string
  secondaryTypes: string[]
  artist: { artistName: string, foreignArtistId: string }
}
const registry = new Map<string, StubAlbum>()

const addAlbum = vi.fn(async (c: { foreignAlbumId: string, artist?: { foreignArtistId?: string } }) => {
  const artistId = c.artist?.foreignArtistId ?? 'unknown'
  const now = (active.get(artistId) ?? 0) + 1
  active.set(artistId, now)
  if (now > 1)
    sameArtistOverlap++
  peakTotal = Math.max(peakTotal, [...active.values()].reduce((a, b) => a + b, 0))
  addOrder.push(`${artistId}:${c.foreignAlbumId}`)
  await new Promise(r => setTimeout(r, 15))
  active.set(artistId, (active.get(artistId) ?? 1) - 1)
  return { id: Number(c.foreignAlbumId.replace(/\D/g, '')) || 1, artistId: Number(artistId.replace(/\D/g, '')) || 1 }
})

vi.mock('../server/utils/lidarr', () => ({
  addAlbum: (c: never, o: never) => addAlbum(c, o),
  addArtist: vi.fn(async () => ({ id: 1 })),
  commandSearchAlbum: vi.fn(async () => undefined),
  commandSearchArtist: vi.fn(async () => undefined),
  monitorAlbums: vi.fn(async () => undefined),
  nudgeExisting: vi.fn(async () => 'nudged'),
  // Identity here: these tests are about add scheduling, not root-folder
  // reconciliation, which tests/lidarr.test.ts covers directly.
  resolveRootFolderPath: vi.fn(async (p: string) => p),
  waitForArtistRefresh: vi.fn(async () => ({ timedOut: false })),
  lookupArtist: vi.fn(async () => []),
  // The job now pre-checks Lidarr's existing library; an empty one keeps these
  // tests focused on add scheduling.
  libraryArtists: vi.fn(async () => []),
  libraryAlbums: vi.fn(async () => []),
  // albumQueryVariations' first term is `${artist} ${title}`, so the registry the
  // test builds in row() is keyed on exactly that.
  lookupAlbum: vi.fn(async (term: string) => {
    const hit = registry.get(term)
    return hit ? [hit] : []
  }),
}))
vi.mock('../server/utils/settings', () => ({
  loadSettings: async () => ({
    rootFolderPath: '/data/music',
    qualityProfileId: 1,
    metadataProfileId: 1,
    monitorMode: 'all' as const,
  }),
}))
vi.mock('../server/utils/history', () => ({
  recordJob: async () => undefined,
  pruneHistory: async () => undefined,
}))
vi.mock('../server/utils/artist-resolve', () => ({
  resolveAlbumViaArtist: async () => null,
  resolveByCandidateAlias: async () => undefined,
  learnArtistIdentity: () => undefined,
  learnedArtistIdentity: () => undefined,
}))
vi.mock('../server/utils/metadata', () => ({
  resolveVariousArtistsAlbumMbids: async () => [],
}))

const { createJob, subscribe } = await import('../server/utils/jobs')

function row(artist: string, title: string): ParsedItem {
  registry.set(`${artist} ${title}`, {
    foreignAlbumId: `alb-${artist}-${title}`,
    title,
    albumType: 'Album',
    secondaryTypes: [],
    artist: { artistName: artist, foreignArtistId: `art-${artist}` },
  })
  return { raw: `${artist} - ${title}`, kind: 'album', artist, title }
}

function runToCompletion(items: ParsedItem[]): Promise<JobSnapshot> {
  return new Promise((resolve) => {
    const snap = createJob('album', items, 'all')
    let latest = snap
    const unsub = subscribe(snap.id, (ev) => {
      if (ev.type === 'snapshot')
        latest = ev.snapshot
      if (ev.type === 'item') {
        const at = latest.items.findIndex(i => i.id === ev.item.id)
        if (at >= 0)
          latest.items[at] = ev.item
      }
      if (ev.type === 'done') {
        unsub?.()
        resolve(latest)
      }
    })
  })
}

beforeEach(() => {
  active.clear()
  peakTotal = 0
  sameArtistOverlap = 0
  addOrder.length = 0
  registry.clear()
  addAlbum.mockClear()
})

describe('add phase scheduling', () => {
  it('never overlaps two adds for the same artist', async () => {
    // Six albums by one artist: each add enqueues a RefreshArtist for that artist
    // that can unmonitor the album just added, and waitForArtistRefresh is
    // per-artist — so these must not run concurrently.
    const items = Array.from({ length: 6 }, (_, i) => row('Darkthrone', `Album ${i}`))
    const done = await runToCompletion(items)
    expect(addAlbum).toHaveBeenCalledTimes(6)
    expect(sameArtistOverlap).toBe(0)
    expect(peakTotal).toBe(1)
    expect(done.items.every(i => i.status === 'done')).toBe(true)
  })

  it('runs adds for different artists concurrently, up to the cap', async () => {
    const items = ['Darkthrone', 'Bathory', 'Mayhem', 'Immortal', 'Burzum', 'Enslaved']
      .map(a => row(a, 'Only Album'))
    await runToCompletion(items)
    expect(addAlbum).toHaveBeenCalledTimes(6)
    expect(sameArtistOverlap).toBe(0)
    expect(peakTotal).toBeGreaterThan(1)
    expect(peakTotal).toBeLessThanOrEqual(3)
  })

  it('parallelises across artists while serialising within each', async () => {
    // Three artists × three albums: overlap across artists, none within.
    const items = ['Darkthrone', 'Bathory', 'Mayhem']
      .flatMap(a => [0, 1, 2].map(i => row(a, `Album ${i}`)))
    const done = await runToCompletion(items)
    expect(addAlbum).toHaveBeenCalledTimes(9)
    expect(sameArtistOverlap).toBe(0)
    expect(peakTotal).toBeGreaterThan(1)
    expect(peakTotal).toBeLessThanOrEqual(3)
    expect(done.items.every(i => i.status === 'done')).toBe(true)
  })

  it('completes every row exactly once', async () => {
    const items = ['A', 'B', 'C', 'D'].flatMap(a => [0, 1].map(i => row(a, `T${i}`)))
    await runToCompletion(items)
    expect(new Set(addOrder).size).toBe(addOrder.length)
    expect(addOrder).toHaveLength(8)
  })
})
