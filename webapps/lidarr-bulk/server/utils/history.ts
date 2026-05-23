// Append-only JSONL history of completed jobs at /config/jobs.jsonl. Cheap to
// write (one line per job), cheap to read (tail), no DB.

import { appendFile, open, readFile } from 'node:fs/promises'
import { join } from 'node:path'
import type { HistoryEntry, ItemStatus, JobSnapshot } from '~~/shared/types'
import { loadEnv } from './env'

const MAX_RETURN = 200

function path(): string {
  return join(loadEnv().CONFIG_DIR, 'jobs.jsonl')
}

export async function recordJob(snap: JobSnapshot): Promise<void> {
  const counts: Partial<Record<ItemStatus, number>> = {}
  for (const it of snap.items)
    counts[it.status] = (counts[it.status] ?? 0) + 1
  const entry: HistoryEntry = {
    id: snap.id,
    createdAt: snap.createdAt,
    finishedAt: Date.now(),
    kind: snap.kind,
    dryRun: snap.dryRun,
    counts,
    items: snap.items.map(it => ({
      id: it.id,
      parsed: it.parsed,
      status: it.status,
      message: it.message,
      chosen: it.chosen,
    })),
  }
  const line = `${JSON.stringify(entry)}\n`
  await appendFile(path(), line, 'utf8')
}

export async function listHistory(limit = MAX_RETURN): Promise<HistoryEntry[]> {
  let buf: string
  try {
    buf = await readFile(path(), 'utf8')
  }
  catch (err: unknown) {
    if ((err as { code?: string }).code === 'ENOENT')
      return []
    throw err
  }
  const lines = buf.split('\n').filter(Boolean)
  const tail = lines.slice(-limit)
  // Newest first.
  return tail
    .map((line) => {
      try {
        return JSON.parse(line) as HistoryEntry
      }
      catch {
        return null
      }
    })
    .filter((e): e is HistoryEntry => e !== null)
    .reverse()
}

export async function findHistoryEntry(id: string): Promise<HistoryEntry | undefined> {
  // O(n) — fine at thousands of entries.
  const all = await listHistory(10_000)
  return all.find(e => e.id === id)
}

// Truncate the file to keep at most `keep` lines. Run periodically by the
// caller (e.g. on each recordJob if file is getting big). Cheap enough that
// we call it inline.
export async function pruneHistory(keep = 1000): Promise<void> {
  let buf: string
  try {
    buf = await readFile(path(), 'utf8')
  }
  catch (err: unknown) {
    if ((err as { code?: string }).code === 'ENOENT')
      return
    throw err
  }
  const lines = buf.split('\n').filter(Boolean)
  if (lines.length <= keep)
    return
  const trimmed = `${lines.slice(-keep).join('\n')}\n`
  // Atomic-ish: open with truncation then write.
  const fh = await open(path(), 'w')
  try {
    await fh.writeFile(trimmed, 'utf8')
  }
  finally {
    await fh.close()
  }
}
