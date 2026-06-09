// In-memory job store + sequential worker. Single-replica only.

import { randomUUID } from 'node:crypto'
import type {
  AppSettings,
  Candidate,
  JobItem,
  JobSnapshot,
  Kind,
  ParsedItem,
} from '~~/shared/types'
import { pruneHistory, recordJob } from './history'
import {
  addAlbum,
  addArtist,
  commandSearchAlbum,
  commandSearchArtist,
  lookupAlbum,
  lookupArtist,
  monitorAlbums,
  nudgeExisting,
  waitForArtistRefresh,
} from './lidarr'
import { normKeyLoose, pickAutoMatch, rankCandidates, similarity } from './matching'
import { loadSettings } from './settings'

// Lidarr's /album/lookup proxies MusicBrainz, which throttles per-IP at ~1 req/s
// in the worst case. 6 in flight saturates Lidarr's local cache without
// flooding the upstream.
const LOOKUP_CONCURRENCY = 6

type Listener = (snap: JobSnapshot) => void

interface JobInternal extends JobSnapshot {
  listeners: Set<Listener>
  // resolved when user picks a candidate or skips (per item id)
  pending: Map<string, (chosen: Candidate | null) => void>
  // promises retained so phase B can await a pick that phase A already
  // registered. Without this the user can only pick the item phase B is
  // currently waiting on; all other 'needs-choice' clicks 404.
  picks: Map<string, Promise<Candidate | null>>
}

export interface JobOptions {
  dryRun?: boolean
  metadataProfileId?: number
  qualityProfileId?: number
}

const jobs = new Map<string, JobInternal>()

function snap(j: JobInternal): JobSnapshot {
  return {
    id: j.id,
    createdAt: j.createdAt,
    kind: j.kind,
    monitorMode: j.monitorMode,
    dryRun: j.dryRun,
    metadataProfileId: j.metadataProfileId,
    qualityProfileId: j.qualityProfileId,
    items: j.items.map(i => ({ ...i })),
    done: j.done,
  }
}

function emit(j: JobInternal): void {
  const s = snap(j)
  for (const l of j.listeners) {
    try {
      l(s)
    }
    catch {
      // ignore listener errors
    }
  }
}

function setStatus(j: JobInternal, item: JobItem, patch: Partial<JobItem>): void {
  Object.assign(item, patch)
  emit(j)
}

export function createJob(
  kind: Kind,
  parsed: ParsedItem[],
  monitorMode: 'all' | 'future',
  opts: JobOptions = {},
): JobSnapshot {
  const job: JobInternal = {
    id: randomUUID(),
    createdAt: Date.now(),
    kind,
    monitorMode,
    dryRun: opts.dryRun ?? false,
    metadataProfileId: opts.metadataProfileId,
    qualityProfileId: opts.qualityProfileId,
    items: parsed.map(p => ({
      id: randomUUID(),
      parsed: p,
      status: 'parsed',
    })),
    done: false,
    listeners: new Set(),
    pending: new Map(),
    picks: new Map(),
  }
  jobs.set(job.id, job)
  // Fire-and-forget; SSE consumers see progress.
  void run(job).catch((err: unknown) => {
    console.error('[job]', job.id, 'crashed:', err)
    job.done = true
    emit(job)
  })
  return snap(job)
}

export function getJob(id: string): JobSnapshot | undefined {
  const j = jobs.get(id)
  return j ? snap(j) : undefined
}

export function subscribe(id: string, listener: Listener): (() => void) | undefined {
  const j = jobs.get(id)
  if (!j)
    return
  j.listeners.add(listener)
  // Push current state immediately.
  listener(snap(j))
  return () => j.listeners.delete(listener)
}

export function choose(jobId: string, itemId: string, candidate: Candidate | null): boolean {
  const j = jobs.get(jobId)
  if (!j)
    return false
  const resolver = j.pending.get(itemId)
  if (!resolver)
    return false
  j.pending.delete(itemId)
  resolver(candidate)
  return true
}

async function run(j: JobInternal): Promise<void> {
  const settings = await loadSettings()
  const effective: AppSettings = {
    ...settings,
    qualityProfileId: j.qualityProfileId ?? settings.qualityProfileId,
    metadataProfileId: j.metadataProfileId ?? settings.metadataProfileId,
  }

  // Phase 0 — eagerly flag needsReview items as needs-choice + spawn pick
  // handlers so user clicks land regardless of phase B's current position.
  for (const item of j.items) {
    if (item.parsed.needsReview) {
      setStatus(j, item, { status: 'needs-choice', message: 'ambiguous input — edit and retry' })
      spawnPickHandler(j, item)
    }
  }

  // Phase A — parallel lookups + auto-match for non-needsReview items. Sets
  // status to matched / needs-choice / not-found / error. needs-choice items
  // get a pick handler so user clicks land regardless of phase B's position.
  const lookupItems = j.items.filter(i => !i.parsed.needsReview)
  await mapWithConcurrency(lookupItems, LOOKUP_CONCURRENCY, async (item) => {
    try {
      setStatus(j, item, { status: 'searching' })
      const candidates = await searchCandidates(j.kind, item.parsed)
      if (candidates.length === 0) {
        setStatus(j, item, { status: 'not-found' })
        return
      }
      const auto = pickAutoMatch(j.kind, item.parsed, candidates)
      if (auto) {
        setStatus(j, item, { status: 'matched', chosen: auto })
      }
      else {
        setStatus(j, item, {
          status: 'needs-choice',
          candidates: rankCandidates(j.kind, item.parsed, candidates),
        })
        spawnPickHandler(j, item)
      }
    }
    catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err)
      setStatus(j, item, { status: 'error', message: msg })
    }
  })

  // Phase B — serial add worker, but drives by readiness, not input order.
  // Pulls any 'matched' item first; if none, awaits the next pick to resolve
  // (which the pick handler will flip to 'matched' or 'skipped'). Adds stay
  // serial — Lidarr's artist-creation + RefreshArtist wait can't overlap.
  while (j.items.some(i => i.status === 'matched' || i.status === 'needs-choice')) {
    const ready = j.items.find(i => i.status === 'matched')
    if (ready) {
      await processAdd(j, ready, effective)
      continue
    }
    const pendingPicks = j.items
      .filter(i => i.status === 'needs-choice')
      .map(i => j.picks.get(i.id))
      .filter((p): p is Promise<Candidate | null> => p !== undefined)
    if (pendingPicks.length === 0)
      break
    await Promise.race(pendingPicks)
  }
  j.done = true
  emit(j)
  // Record to history file once the job is done (any pending picks turned into
  // 'skipped' via the loop above). Pruning is cheap; we keep ~1000 entries.
  try {
    await recordJob(snap(j))
    await pruneHistory(1000)
  }
  catch (err) {
    console.error('[job]', j.id, 'history record failed:', err)
  }
}

function registerPick(j: JobInternal, itemId: string): Promise<Candidate | null> {
  const existing = j.picks.get(itemId)
  if (existing)
    return existing
  const p = new Promise<Candidate | null>((resolve) => {
    j.pending.set(itemId, resolve)
  })
  j.picks.set(itemId, p)
  return p
}

// Spawns a background promise that flips item.status once the user picks. This
// is what makes the phase-B worker loop able to find the next 'matched' item
// without iterating in input order.
function spawnPickHandler(j: JobInternal, item: JobItem): void {
  void registerPick(j, item.id).then((picked) => {
    if (picked)
      setStatus(j, item, { status: 'matched', chosen: picked, candidates: undefined })
    else
      setStatus(j, item, { status: 'skipped' })
  })
}

async function processAdd(j: JobInternal, item: JobItem, effective: AppSettings): Promise<void> {
  try {
    if (j.dryRun) {
      setStatus(j, item, { status: 'would-add' })
      return
    }
    if (!item.chosen) {
      setStatus(j, item, { status: 'skipped' })
      return
    }
    setStatus(j, item, { status: 'adding' })
    const added = await addToLidarr(item.chosen, effective, j.monitorMode)
    setStatus(j, item, { status: 'searching-on-lidarr' })
    if (j.kind === 'album' && added.albumId && added.artistId) {
      await waitForArtistRefresh(added.artistId).catch(() => undefined)
      await monitorAlbums([added.albumId], true).catch(() => undefined)
    }
    setStatus(j, item, { status: 'done' })
  }
  catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    // Already in Lidarr is no longer a dead end: nudge the existing record
    // (force it monitored, kick a search for what's missing) rather than just
    // shrugging. A failure inside the nudge surfaces as an error so it's visible.
    if (/already been added/i.test(msg) && item.chosen) {
      try {
        const summary = await nudgeExisting(item.chosen, j.monitorMode)
        setStatus(j, item, { status: 'nudged', message: summary })
      }
      catch (nudgeErr: unknown) {
        const nudgeMsg = nudgeErr instanceof Error ? nudgeErr.message : String(nudgeErr)
        setStatus(j, item, { status: 'error', message: `already in lidarr but nudge failed: ${nudgeMsg}` })
      }
    }
    else {
      setStatus(j, item, { status: 'error', message: msg })
    }
  }
}

export async function mapWithConcurrency<T, R>(
  items: T[],
  concurrency: number,
  fn: (item: T, index: number) => Promise<R>,
): Promise<R[]> {
  if (items.length === 0)
    return []
  const out: R[] = Array.from({ length: items.length })
  let next = 0
  const workerCount = Math.min(Math.max(1, concurrency), items.length)
  const workers: Promise<void>[] = []
  for (let w = 0; w < workerCount; w++) {
    workers.push((async () => {
      while (true) {
        const i = next++
        if (i >= items.length)
          return
        out[i] = await fn(items[i]!, i)
      }
    })())
  }
  await Promise.all(workers)
  return out
}

// Lidarr's lookup proxies MusicBrainz / api.lidarr.audio; both regularly emit
// 5xx with bodies like "Invalid response received from LidarrAPI" or
// "Unable to communicate with LidarrAPI" — every one of those is transient and
// clears within seconds. Retry 3x with exponential backoff before giving up.
const TRANSIENT_LOOKUP_ERROR = /\b50[0-9]\b|Invalid response received|Unable to communicate/i

async function retryOnTransient<T>(fn: () => Promise<T>, attempts = 3): Promise<T> {
  let lastErr: unknown
  for (let i = 0; i < attempts; i++) {
    try {
      return await fn()
    }
    catch (err) {
      lastErr = err
      const msg = err instanceof Error ? err.message : String(err)
      if (!TRANSIENT_LOOKUP_ERROR.test(msg) || i === attempts - 1)
        throw err
      await new Promise(r => setTimeout(r, 1000 * 2 ** i))
    }
  }
  throw lastErr instanceof Error ? lastErr : new Error(String(lastErr))
}

async function searchCandidates(kind: Kind, parsed: ParsedItem): Promise<Candidate[]> {
  if (kind === 'artist') {
    const res = await retryOnTransient(() => lookupArtist(parsed.raw))
    return res.map(value => ({ kind: 'artist', value }))
  }
  const term = parsed.artist && parsed.title
    ? `${parsed.artist} ${parsed.title}`
    : parsed.raw
  const primary = await retryOnTransient(() => lookupAlbum(term))

  // Fallback: if nothing in the primary result is title-similar to what was
  // typed AND the title carries a parens/bracket qualifier, re-query with the
  // qualifier stripped. Catches "Carnal Leftovers (demos)" → "Carnal Leftovers"
  // and merges any new hits in.
  if (parsed.title && /[([]/.test(parsed.title)) {
    const wantTitle = normKeyLoose(parsed.title)
    const hasGoodMatch = primary.some(c => similarity(normKeyLoose(c.title), wantTitle) > 0.8)
    if (!hasGoodMatch) {
      const stripped = parsed.title.replace(/[([][^)\]]*[)\]]/g, ' ').replace(/\s+/g, ' ').trim()
      if (stripped && stripped !== parsed.title) {
        const fallbackTerm = parsed.artist ? `${parsed.artist} ${stripped}` : stripped
        const fallback = await retryOnTransient(() => lookupAlbum(fallbackTerm)).catch(() => [] as typeof primary)
        const seen = new Set(primary.map(r => r.foreignAlbumId))
        for (const c of fallback) {
          if (!seen.has(c.foreignAlbumId)) {
            primary.push(c)
            seen.add(c.foreignAlbumId)
          }
        }
      }
    }
  }

  return primary.map(value => ({ kind: 'album', value }))
}

async function addToLidarr(
  chosen: Candidate,
  settings: AppSettings,
  monitorMode: 'all' | 'future',
): Promise<{ albumId?: number, artistId?: number }> {
  const opts = {
    rootFolderPath: settings.rootFolderPath,
    qualityProfileId: settings.qualityProfileId,
    metadataProfileId: settings.metadataProfileId,
    monitorMode,
    searchOnAdd: true,
  }
  if (chosen.kind === 'artist') {
    const r = await addArtist(chosen.value, opts)
    if (r?.id)
      await commandSearchArtist(r.id).catch(() => undefined)
    return { artistId: r?.id }
  }
  const r = await addAlbum(chosen.value, opts)
  if (r?.id)
    await commandSearchAlbum([r.id]).catch(() => undefined)
  return { albumId: r?.id, artistId: r?.artistId }
}
