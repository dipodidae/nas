// Client for Lidarr's metadata backend (api.lidarr.audio). Used only to resolve
// Various Artists compilations: Lidarr's own /album/lookup deliberately hides the
// special VA entity from text search, but the backend's raw search returns it, so
// we search by title here, keep the VA-credited albums, and hand their MBIDs back
// to the normal lookup-by-MBID + add pipeline. Pure ranking is split from the
// fetch so it can be unit-tested (mirrors spotify.ts / openai.ts).
import { loadEnv } from './env'
import { normKey, similarity } from './matching'

export const VARIOUS_ARTISTS_MBID = '89ad4ac3-39f7-470e-963a-56509c546377'

interface MetaAlbum { id?: unknown, title?: unknown, artistid?: unknown, releasedate?: unknown }
interface MetaSearchEntry { album?: MetaAlbum | null }
export interface VaAlbumMatch { mbid: string, title: string, year?: number }

function year(v: unknown): number | undefined {
  if (typeof v !== 'string')
    return undefined
  const y = Number.parseInt(v.slice(0, 4), 10)
  return Number.isFinite(y) ? y : undefined
}

export function rankVaAlbums(entries: MetaSearchEntry[], title: string, want?: number, limit = 5): VaAlbumMatch[] {
  const target = normKey(title)
  const scored = entries
    .map(e => e.album)
    .filter((a): a is { id: string, title: string, artistid: string, releasedate?: string } =>
      !!a && a.artistid === VARIOUS_ARTISTS_MBID && typeof a.id === 'string' && typeof a.title === 'string')
    .map((a) => {
      const y = year(a.releasedate)
      // Title similarity dominates; year proximity is a small tiebreak (<=0.05).
      const yearPenalty = want !== undefined && y !== undefined ? Math.min(Math.abs(y - want), 50) / 1000 : 0
      return { mbid: a.id, title: a.title, year: y, score: similarity(normKey(a.title), target) - yearPenalty }
    })
    .sort((x, z) => z.score - x.score)
  const seen = new Set<string>()
  const out: VaAlbumMatch[] = []
  for (const m of scored) {
    if (seen.has(m.mbid))
      continue
    seen.add(m.mbid)
    out.push({ mbid: m.mbid, title: m.title, year: m.year })
    if (out.length >= limit)
      break
  }
  return out
}

export async function resolveVariousArtistsAlbumMbids(title: string, want?: number, limit = 5): Promise<string[]> {
  const base = loadEnv().LIDARR_METADATA_URL.replace(/\/$/, '')
  const url = `${base}/search?type=all&query=${encodeURIComponent(title)}`
  const res = await fetch(url, { headers: { 'User-Agent': 'lidarr-bulk' } })
  if (!res.ok)
    throw new Error(`metadata search failed (${res.status})`)
  const body = await res.json() as MetaSearchEntry[]
  return rankVaAlbums(Array.isArray(body) ? body : [], title, want, limit).map(m => m.mbid)
}
