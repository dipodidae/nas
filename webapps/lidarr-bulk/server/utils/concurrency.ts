// Bounded parallel map, order-preserving. Shared by the job pipeline and the
// Jellyfin recreate path, both of which fan out over thousands of items against
// APIs that must not be flooded.

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
