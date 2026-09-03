// In-memory job store + sequential worker. Single-replica only.

import { randomUUID } from 'node:crypto'
import type {
  AppSettings,
  Candidate,
  JobItem,
  JobSnapshot,
  Kind,
  LidarrAlbumCandidate,
  ParsedItem,
} from '~~/shared/types'
import { mapWithConcurrency } from './concurrency'
import { findExistingAlbum } from './library'
import { learnArtistIdentity, learnedArtistIdentity, resolveAlbumViaArtist, resolveByCandidateAlias } from './artist-resolve'
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
  resolveRootFolderPath,
  waitForArtistRefresh,
} from './lidarr'
import { albumQueryVariations, hasPlausibleArtist, isVariousArtists, normKeyLoose, pickAutoMatch, rankCandidates } from './matching'
import { similarity } from './text'
import { resolveVariousArtistsAlbumMbids } from './metadata'
import { loadSettings } from './settings'

// Lidarr's /album/lookup proxies MusicBrainz, which throttles per-IP at ~1 req/s
// in the worst case. 6 in flight saturates Lidarr's local cache without
// flooding the upstream.
const LOOKUP_CONCURRENCY = 6

// SSE protocol. emit() used to push a full JobSnapshot on every status change,
// which is O(items) per change and therefore O(items²) per job. With ~900 albums
// and candidate lists attached that reached tens of megabytes per update and made
// the browser unusable. Now the initial state is sent once and every subsequent
// change is a single-item patch.
export type JobEvent =
  | { type: 'snapshot', snapshot: JobSnapshot }
  | { type: 'item', item: JobItem }
  | { type: 'done' }

type Listener = (event: JobEvent) => void

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

function publish(j: JobInternal, event: JobEvent): void {
  for (const l of j.listeners) {
    try {
      l(event)
    }
    catch {
      // ignore listener errors
    }
  }
}

// Whole-job push. Only for the initial subscribe and for completion — never per
// status change; see the JobEvent comment.
function emit(j: JobInternal): void {
  publish(j, j.done ? { type: 'done' } : { type: 'snapshot', snapshot: snap(j) })
}

function setStatus(j: JobInternal, item: JobItem, patch: Partial<JobItem>): void {
  Object.assign(item, patch)
  publish(j, { type: 'item', item: { ...item } })
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
  // Push current state immediately, then only deltas.
  listener({ type: 'snapshot', snapshot: snap(j) })
  if (j.done)
    listener({ type: 'done' })
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
      // Do we already own this? Answering from Lidarr's own library costs one
      // cached artist-index read plus one small per-artist fetch, and when the
      // answer is "yes, complete" it saves the entire MusicBrainz cascade, a
      // doomed add, a nudge, and — most importantly — a pointless download.
      if (j.kind === 'album' && await settleIfAlreadyHeld(j, item))
        return
      const outcome = await searchCandidates(j.kind, item.parsed)
      applyOutcome(j, item, outcome)
    }
    catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err)
      setStatus(j, item, { status: 'error', message: msg })
    }
  })

  // Phase A2 — retry the rows that are still unresolved, now that the resolved
  // ones have taught us artist identities. Lidarr's artist lookup can't find a
  // Cyrillic artist behind a short Latin homograph ("Kino" → ten Latin bands),
  // but a sibling row that matched on its title already identified them. Bounded:
  // one extra attempt per unresolved album row, and only when something was
  // actually learned.
  const retryable = j.items.filter(i =>
    !i.parsed.needsReview
    && i.status === 'needs-choice'
    && !i.chosen
    && j.kind === 'album'
    && learnedArtistIdentity(i.parsed.artist) !== undefined,
  )
  if (retryable.length > 0) {
    await mapWithConcurrency(retryable, LOOKUP_CONCURRENCY, async (item) => {
      // The user may have picked while we were working; never clobber that.
      if (item.status !== 'needs-choice' || item.chosen)
        return
      try {
        const outcome = await searchCandidates(j.kind, item.parsed)
        if (outcome.chosen && item.status === 'needs-choice' && !item.chosen)
          applyOutcome(j, item, outcome)
      }
      catch {
        // Leave the row as-is; the user can still pick from the original list.
      }
    })
  }

  await runAddPhase(j, effective)
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

// Phase B — the add worker. Driven by readiness, not input order: it takes any
// 'matched' item, and when none is ready it waits for the next user pick (which
// the pick handler flips to 'matched' or 'skipped').
//
// Adds run concurrently across artists but strictly sequentially *within* one
// artist. That split is the whole point. Lidarr enqueues a RefreshArtist after
// every add, and that refresh runs AlbumMonitoredService and can unmonitor the
// album we just added — the clobber that waitForArtistRefresh(artistId) exists to
// wait out. The wait is per-artist, so two adds for the *same* artist would race
// each other's refresh, while adds for *different* artists never touch the same
// refresh and are safe in parallel. A playlist is mostly distinct artists, so this
// turns an hours-long serial queue into roughly a third of that without weakening
// the monitoring guarantee at all.
const ADD_CONCURRENCY = 3

function artistKeyOf(item: JobItem): string {
  if (!item.chosen)
    return item.id
  const { mbid, name } = candidateArtist(item.chosen)
  // Fall back to the item id rather than a shared empty key, so unidentifiable
  // rows don't all serialise against each other for no reason.
  return mbid ?? (normKeyLoose(name) || item.id)
}

async function runAddPhase(j: JobInternal, effective: AppSettings): Promise<void> {
  // Artists currently being added to. Guarantees same-artist serialisation.
  const busyArtists = new Set<string>()
  const inFlight = new Set<Promise<void>>()

  const claimable = (): JobItem | undefined =>
    j.items.find(i => i.status === 'matched' && !busyArtists.has(artistKeyOf(i)))

  const outstanding = (): boolean =>
    j.items.some(i => i.status === 'matched' || i.status === 'needs-choice')

  while (outstanding() || inFlight.size > 0) {
    // Fill the pipeline with work whose artist isn't already busy.
    while (inFlight.size < ADD_CONCURRENCY) {
      const item = claimable()
      if (!item)
        break
      const key = artistKeyOf(item)
      busyArtists.add(key)
      // Claim it immediately so the next loop iteration can't pick it up again.
      item.status = 'adding'
      const task = processAdd(j, item, effective)
        .catch((err: unknown) => {
          console.error('[job]', j.id, 'add task crashed:', err)
        })
        .finally(() => {
          busyArtists.delete(key)
          inFlight.delete(task)
        })
      inFlight.add(task)
    }

    if (inFlight.size === 0) {
      // Nothing addable. Either everything left is waiting on the user, or the
      // only ready items belong to artists we're already adding to (impossible
      // here, since inFlight is empty) — so wait for a pick.
      const pendingPicks = j.items
        .filter(i => i.status === 'needs-choice')
        .map(i => j.picks.get(i.id))
        .filter((p): p is Promise<Candidate | null> => p !== undefined)
      if (pendingPicks.length === 0)
        break
      await Promise.race(pendingPicks)
      continue
    }

    // Wake on the first add finishing, or on a pick arriving — whichever is
    // first — so a newly-picked item doesn't wait behind a slow add.
    const pendingPicks = j.items
      .filter(i => i.status === 'needs-choice')
      .map(i => j.picks.get(i.id))
      .filter((p): p is Promise<Candidate | null> => p !== undefined)
    await Promise.race<unknown>([...inFlight, ...pendingPicks])
  }
}

// Short-circuit a row against Lidarr's existing library. Returns true when the row
// is settled and no lookup should happen.
//
//  - already complete on disk → nothing to do at all. Not "added", not "nudged":
//    re-monitoring and re-searching a finished album only burns indexer and
//    Soulseek capacity to re-download something we have.
//  - present but incomplete → we already know its album id, so monitor + search it
//    directly and skip the lookup, the doomed add and the nudge round-trip.
async function settleIfAlreadyHeld(j: JobInternal, item: JobItem): Promise<boolean> {
  const existing = await findExistingAlbum(item.parsed).catch((err: unknown) => {
    console.error('[job] library pre-check failed:', err instanceof Error ? err.message : String(err))
    return null
  })
  if (!existing)
    return false

  if (existing.complete) {
    setStatus(j, item, {
      status: 'already-complete',
      message: `already in lidarr, complete on disk — ${existing.artistName} / ${existing.title}`,
    })
    return true
  }

  if (j.dryRun) {
    setStatus(j, item, {
      status: 'would-add',
      message: `already in lidarr, ${existing.percentOfTracks ?? 0}% on disk — would search`,
    })
    return true
  }

  try {
    await monitorAlbums([existing.albumId], true)
    await commandSearchAlbum([existing.albumId])
    setStatus(j, item, {
      status: 'nudged',
      message: `already in lidarr at ${existing.percentOfTracks ?? 0}% — re-monitored + searching`,
    })
  }
  catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    setStatus(j, item, { status: 'error', message: `nudge of existing album failed: ${msg}` })
  }
  return true
}

function candidateArtist(c: Candidate): { mbid?: string, name?: string } {
  if (c.kind === 'artist')
    return { mbid: c.value.foreignArtistId, name: c.value.artistName }
  const artist = c.value.artist
  return typeof artist === 'string'
    ? { name: artist }
    : { mbid: artist?.foreignArtistId, name: artist?.artistName }
}

// Turn a search outcome into item state. Split out so the phase-A2 retry can
// reuse exactly the same rules rather than re-deriving them.
function applyOutcome(j: JobInternal, item: JobItem, outcome: SearchOutcome): void {
  const { candidates, chosen } = outcome
  // The album cascade already ran the matcher (and, for its later stages,
  // resolved the release outright), so only the artist path decides here.
  const auto = chosen ?? pickAutoMatch(j.kind, item.parsed, candidates)
  if (auto) {
    setStatus(j, item, { status: 'matched', chosen: auto, candidates: undefined })
    // Teach the rest of the job who this artist is.
    const { mbid, name } = candidateArtist(auto)
    learnArtistIdentity(j.kind === 'artist' ? item.parsed.raw : item.parsed.artist, mbid, name)
    return
  }
  if (candidates.length === 0) {
    // An empty result is only "not found" if the lookup actually succeeded.
    // Lidarr's metadata proxy returns 503 under load, and calling that
    // not-found sent perfectly ordinary albums ("Iron Maiden — Somewhere in
    // Time") to the graveyard with no way for the user to tell.
    if (outcome.lookupFailed) {
      setStatus(j, item, {
        status: 'error',
        message: `lookup failed, not absent — ${outcome.lookupError ?? 'upstream error'}`,
      })
      return
    }
    setStatus(j, item, { status: 'not-found' })
    return
  }
  // Nothing here is even by the artist that was asked for. Presenting ten
  // unrelated records as "multiple matches — pick the right one" is worse than
  // saying so: the honest answer is that this release wasn't found, and the user
  // was only ever going to hit Skip.
  if (j.kind === 'album' && outcome.unrelatedResults && !outcome.lookupFailed) {
    setStatus(j, item, {
      status: 'not-found',
      message: `no release by ${item.parsed.artist} found (${candidates.length} unrelated result${candidates.length === 1 ? '' : 's'} discarded)`,
    })
    return
  }
  setStatus(j, item, {
    status: 'needs-choice',
    candidates: rankCandidates(j.kind, item.parsed, candidates),
  })
  spawnPickHandler(j, item)
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
    console.error('[job]', j.id, 'add failed:', msg)
    // Already in Lidarr is no longer a dead end: nudge the existing record.
    if (/already been added/i.test(msg) && item.chosen) {
      try {
        const summary = await nudgeExisting(item.chosen, j.monitorMode)
        setStatus(j, item, { status: 'nudged', message: summary })
      }
      catch (nudgeErr: unknown) {
        const nudgeMsg = nudgeErr instanceof Error ? nudgeErr.message : String(nudgeErr)
        setStatus(j, item, { status: 'error', message: `already in lidarr but nudge failed: ${nudgeMsg}` })
      }
      return
    }
    // Image-fetch failures are usually transient and the record often got created
    // anyway: retry once, and if Lidarr then reports it already added, nudge it.
    if (isImageFetchError(msg) && item.chosen) {
      try {
        await new Promise(r => setTimeout(r, 1500))
        const added = await addToLidarr(item.chosen, effective, j.monitorMode)
        setStatus(j, item, { status: 'searching-on-lidarr' })
        if (j.kind === 'album' && added.albumId && added.artistId) {
          await waitForArtistRefresh(added.artistId).catch(() => undefined)
          await monitorAlbums([added.albumId], true).catch(() => undefined)
        }
        setStatus(j, item, { status: 'done' })
        return
      }
      catch (retryErr: unknown) {
        const retryMsg = retryErr instanceof Error ? retryErr.message : String(retryErr)
        // The first add may have created the record before the image fetch failed.
        // nudgeExisting looks it up by foreign id (and throws if truly absent), so a
        // successful nudge means the record exists and we treat it as added.
        try {
          const summary = await nudgeExisting(item.chosen, j.monitorMode)
          setStatus(j, item, { status: 'nudged', message: `${summary} (image fetch retried)` })
          return
        }
        catch (nudgeErr: unknown) {
          const nudgeMsg = nudgeErr instanceof Error ? nudgeErr.message : String(nudgeErr)
          console.error('[job]', j.id, 'image-retry nudge failed:', nudgeMsg)
          setStatus(j, item, { status: 'error', message: `image-fetch add failed twice: ${retryMsg}` })
          return
        }
      }
    }
    setStatus(j, item, { status: 'error', message: msg })
  }
}


// Lidarr's lookup proxies MusicBrainz / api.lidarr.audio; both regularly emit
// 5xx with bodies like "Invalid response received from LidarrAPI" or
// "Unable to communicate with LidarrAPI" — every one of those is transient and
// clears within seconds. Retry 3x with exponential backoff before giving up.
const TRANSIENT_LOOKUP_ERROR = /\b50[0-9]\b|Invalid response received|Unable to communicate/i

// Lidarr occasionally fails an add while fetching artwork from its metadata
// server. The record itself is usually created; the image is cosmetic. Detect
// these so processAdd can retry once and then verify rather than hard-erroring.
export function isImageFetchError(msg: string): boolean {
  return /image|mediacover|cover art|cover image|failed to (?:download|fetch)/i.test(msg)
}

// Adaptive throttle. The 5xx above are not random: they are what the upstream
// does when we push it too hard, and a 900-album playlist pushes it hard enough
// that ordinary rows ("Iron Maiden Somewhere in Time") start failing. Since a
// failed lookup used to be indistinguishable from an empty one, those rows were
// then reported as "not found" — the flood manufactured its own false negatives.
//
// So every transient failure makes the whole fleet pause briefly. Under healthy
// conditions this costs nothing; under strain it converges on a rate the upstream
// will actually serve instead of hammering it into refusing everything.
const THROTTLE_MS = 4000
let throttleUntil = 0

const sleep = (ms: number): Promise<void> => new Promise(r => setTimeout(r, ms))

async function awaitThrottle(): Promise<void> {
  const wait = throttleUntil - Date.now()
  if (wait > 0)
    await sleep(wait)
}

function noteTransientFailure(): void {
  throttleUntil = Math.max(throttleUntil, Date.now() + THROTTLE_MS)
}

export function resetLookupThrottle(): void {
  throttleUntil = 0
}

async function retryOnTransient<T>(fn: () => Promise<T>, attempts = 3): Promise<T> {
  let lastErr: unknown
  for (let i = 0; i < attempts; i++) {
    await awaitThrottle()
    try {
      return await fn()
    }
    catch (err) {
      lastErr = err
      const msg = err instanceof Error ? err.message : String(err)
      if (!TRANSIENT_LOOKUP_ERROR.test(msg))
        throw err
      noteTransientFailure()
      if (i === attempts - 1)
        throw err
      await sleep(1000 * 2 ** i)
    }
  }
  throw lastErr instanceof Error ? lastErr : new Error(String(lastErr))
}

// Marks a lookup as having failed rather than returned nothing. `[]` from a 503 is
// not evidence of absence, and reporting it as "not found" is a lie the user can't
// see through — so the row becomes a retryable error instead.
interface LookupFailure { failed: boolean, message?: string }

async function tryLookup<T>(
  fn: () => Promise<T[]>,
  failure: LookupFailure,
  label: string,
): Promise<T[]> {
  try {
    return await retryOnTransient(fn)
  }
  catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    failure.failed = true
    failure.message ??= msg
    console.error('[job] lookup failed', label, msg)
    return []
  }
}

// Result of the album search cascade. `chosen` is set when a later, more
// authoritative stage didn't just retrieve candidates but actually identified the
// release — the caller then skips auto-matching rather than re-deciding on
// weaker evidence.
export interface SearchOutcome {
  candidates: Candidate[]
  chosen?: Candidate
  // True when not one candidate is even plausibly by the requested artist, so
  // the result set is unrelated noise rather than a set of alternatives.
  unrelatedResults?: boolean
  // Set when a lookup errored out rather than returning nothing. An empty result
  // from a 503 must never be presented as "this release does not exist".
  lookupFailed?: boolean
  lookupError?: string
}

// Turn a MusicBrainz album id back into a real Lidarr candidate so the add path
// (which needs foreignAlbumId plus the nested artist record) is unchanged.
async function lookupByMbid(mbid: string, failure: LookupFailure): Promise<Candidate[]> {
  const res = await tryLookup(() => lookupAlbum(`lidarr:${mbid}`), failure, `mbid ${mbid}`)
  return res.map(value => ({ kind: 'album' as const, value }))
}

async function searchCandidates(kind: Kind, parsed: ParsedItem): Promise<SearchOutcome> {
  const failure: LookupFailure = { failed: false }
  if (kind === 'artist') {
    const res = await tryLookup(() => lookupArtist(parsed.raw), failure, parsed.raw)
    return {
      candidates: res.map(value => ({ kind: 'artist', value })),
      lookupFailed: failure.failed,
      lookupError: failure.message,
    }
  }

  // Various Artists compilations: Lidarr text search hides the special VA entity,
  // so resolve the comp's MBID via the metadata backend and look it up by id.
  if (parsed.variousArtists || isVariousArtists(parsed.artist)) {
    const mbids = await resolveVariousArtistsAlbumMbids(parsed.title ?? parsed.raw, parsed.year)
      .catch((err: unknown) => {
        console.error('[job] VA resolve failed:', err instanceof Error ? err.message : String(err))
        return [] as string[]
      })
    const looked = await Promise.all(
      mbids.map(mbid => tryLookup(() => lookupAlbum(`lidarr:${mbid}`), failure, `VA ${mbid}`)),
    )
    const seen = new Set<string>()
    const out: Candidate[] = []
    for (const value of looked.flat()) {
      if (seen.has(value.foreignAlbumId))
        continue
      seen.add(value.foreignAlbumId)
      out.push({ kind: 'album', value })
    }
    return { candidates: out, lookupFailed: failure.failed, lookupError: failure.message }
  }

  return searchAlbumCandidates(parsed)
}

// Album retrieval cascade. Each stage is strictly more authoritative and strictly
// more expensive than the last, and every stage short-circuits as soon as the
// matcher can commit — so ordinary Latin rows still cost exactly one lookup.
//
//   1. Text search over query variations. albumQueryVariations leads with the
//      bare native-script title for mixed-script rows, because a combined
//      "Basta На заре" term is poison: the search discards the Cyrillic half and
//      matches "Basta" against unrelated Swedish "Bästa" records.
//   2. Candidate-side alias verification. We already hold rows whose title is an
//      exact hit; the open question is only whether one of those artists is the
//      one asked for. Settles "Мираж" ≡ Spotify's "Mirage".
//   3. Artist-first discography resolution. For when the search never returned
//      the album at all — a Latin title under a Cyrillic artist, or a canonical
//      album buried under tribute pressings ("Pink Floyd The Wall").
export async function searchAlbumCandidates(parsed: ParsedItem): Promise<SearchOutcome> {
  const failure: LookupFailure = { failed: false }
  const variations = albumQueryVariations(parsed)
  const want = normKeyLoose(parsed.title ?? parsed.raw)
  const merged: LidarrAlbumCandidate[] = []
  const seen = new Set<string>()
  for (const term of variations) {
    const res = await tryLookup(() => lookupAlbum(term), failure, term)
    for (const c of res) {
      if (!seen.has(c.foreignAlbumId)) {
        seen.add(c.foreignAlbumId)
        merged.push(c)
      }
    }
    if (merged.some(c => similarity(normKeyLoose(c.title), want) > 0.8))
      break
  }
  const candidates: Candidate[] = merged.map(value => ({ kind: 'album', value }))

  const direct = pickAutoMatch('album', parsed, candidates)
  if (direct)
    return { candidates, chosen: direct }

  const aliasHit = await resolveByCandidateAlias(parsed, candidates).catch((err: unknown) => {
    console.error('[job] alias verify failed:', err instanceof Error ? err.message : String(err))
    return undefined
  })
  if (aliasHit)
    return { candidates, chosen: aliasHit }

  const viaArtist = await resolveAlbumViaArtist(parsed).catch((err: unknown) => {
    console.error('[job] artist-first resolve failed:', err instanceof Error ? err.message : String(err))
    return null
  })
  if (viaArtist) {
    const resolved = await lookupByMbid(viaArtist.album.mbid, failure)
    const chosen = resolved[0]
    if (chosen) {
      // Keep the original hits in the list behind the resolved one, so a wrong
      // resolution is still correctable from the picker rather than a dead end.
      const extra = candidates.filter(c =>
        c.kind === 'album' && c.value.foreignAlbumId !== viaArtist.album.mbid)
      return { candidates: [chosen, ...extra], chosen }
    }
  }

  return {
    candidates,
    unrelatedResults: !hasPlausibleArtist(parsed, candidates),
    lookupFailed: failure.failed,
    lookupError: failure.message,
  }
}

async function addToLidarr(
  chosen: Candidate,
  settings: AppSettings,
  monitorMode: 'all' | 'future',
): Promise<{ albumId?: number, artistId?: number }> {
  const opts = {
    // Reconciled against Lidarr's live roots, not trusted from settings.json:
    // a stale value there is a hard 400 from Lidarr. See ADR-0003.
    rootFolderPath: await resolveRootFolderPath(settings.rootFolderPath),
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
