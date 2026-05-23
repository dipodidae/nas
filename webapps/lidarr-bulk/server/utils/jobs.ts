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
} from './lidarr'
import { loadSettings } from './settings'

type Listener = (snap: JobSnapshot) => void

interface JobInternal extends JobSnapshot {
  listeners: Set<Listener>
  // resolved when user picks a candidate or skips (per item id)
  pending: Map<string, (chosen: Candidate | null) => void>
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

  for (const item of j.items) {
    try {
      if (item.parsed.needsReview) {
        setStatus(j, item, { status: 'needs-choice', message: 'ambiguous input — edit and retry' })
        const picked = await waitForChoice(j, item.id)
        if (!picked) {
          setStatus(j, item, { status: 'skipped' })
          continue
        }
        item.chosen = picked
      }

      setStatus(j, item, { status: 'searching' })
      const candidates = await searchCandidates(j.kind, item.parsed)
      if (candidates.length === 0) {
        setStatus(j, item, { status: 'not-found' })
        continue
      }

      const auto = pickAutoMatch(j.kind, item.parsed, candidates)
      let chosen: Candidate
      if (auto) {
        chosen = auto
      }
      else {
        setStatus(j, item, { status: 'needs-choice', candidates })
        const picked = await waitForChoice(j, item.id)
        if (!picked) {
          setStatus(j, item, { status: 'skipped' })
          continue
        }
        chosen = picked
      }

      setStatus(j, item, { status: 'matched', chosen, candidates: undefined })

      if (j.dryRun) {
        setStatus(j, item, { status: 'would-add' })
        continue
      }

      setStatus(j, item, { status: 'adding' })
      await addToLidarr(chosen, effective, j.monitorMode)
      setStatus(j, item, { status: 'searching-on-lidarr' })
      setStatus(j, item, { status: 'done' })
    }
    catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err)
      if (/already been added/i.test(msg))
        setStatus(j, item, { status: 'already-added' })
      else
        setStatus(j, item, { status: 'error', message: msg })
    }
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

function waitForChoice(j: JobInternal, itemId: string): Promise<Candidate | null> {
  return new Promise((resolve) => {
    j.pending.set(itemId, resolve)
  })
}

function norm(s: string | undefined): string {
  return (s ?? '').toLowerCase().trim().replace(/\s+/g, ' ')
}

function pickAutoMatch(
  kind: Kind,
  parsed: ParsedItem,
  candidates: Candidate[],
): Candidate | undefined {
  if (candidates.length === 1)
    return candidates[0]
  const top = candidates[0]
  if (!top)
    return undefined
  if (kind === 'artist') {
    if (top.kind !== 'artist')
      return undefined
    if (norm(top.value.artistName) === norm(parsed.raw))
      return top
    return undefined
  }
  // album
  if (top.kind !== 'album')
    return undefined
  const v = top.value
  const cArtist = typeof v.artist === 'string' ? v.artist : v.artist?.artistName
  if (
    parsed.artist
    && parsed.title
    && norm(v.title) === norm(parsed.title)
    && norm(cArtist) === norm(parsed.artist)
  )
    return top
  return undefined
}

async function searchCandidates(kind: Kind, parsed: ParsedItem): Promise<Candidate[]> {
  if (kind === 'artist') {
    const res = await lookupArtist(parsed.raw)
    return res.map(value => ({ kind: 'artist', value }))
  }
  const term = parsed.artist && parsed.title
    ? `${parsed.artist} ${parsed.title}`
    : parsed.raw
  const res = await lookupAlbum(term)
  return res.map(value => ({ kind: 'album', value }))
}

async function addToLidarr(
  chosen: Candidate,
  settings: AppSettings,
  monitorMode: 'all' | 'future',
): Promise<void> {
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
  }
  else {
    const r = await addAlbum(chosen.value, opts)
    if (r?.id)
      await commandSearchAlbum([r.id]).catch(() => undefined)
  }
}
