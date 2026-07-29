import { describe, expect, it } from 'vitest'
import { mapWithConcurrency } from '../server/utils/concurrency'
import { isImageFetchError } from '../server/utils/jobs'

describe('mapWithConcurrency', () => {
  it('returns [] for an empty input without spawning workers', async () => {
    const fn = async () => 1
    expect(await mapWithConcurrency([], 6, fn)).toEqual([])
  })

  it('preserves input order in the output array', async () => {
    const items = [10, 20, 30, 40, 50]
    const out = await mapWithConcurrency(items, 3, async (n) => {
      await new Promise(r => setTimeout(r, Math.random() * 5))
      return n * 2
    })
    expect(out).toEqual([20, 40, 60, 80, 100])
  })

  it('never runs more than `concurrency` calls at the same time', async () => {
    let inFlight = 0
    let peak = 0
    const items = Array.from({ length: 20 }, (_, i) => i)
    await mapWithConcurrency(items, 4, async () => {
      inFlight++
      peak = Math.max(peak, inFlight)
      await new Promise(r => setTimeout(r, 5))
      inFlight--
      return null
    })
    expect(peak).toBeLessThanOrEqual(4)
    expect(peak).toBeGreaterThan(1)
  })
})

describe('isImageFetchError', () => {
  it('matches Lidarr image/cover fetch failures', () => {
    for (const m of [
      'Lidarr 500: failed to download image',
      'Error fetching cover art for album',
      'MediaCover refresh failed',
    ])
      expect(isImageFetchError(m)).toBe(true)
  })
  it('does not match unrelated errors', () => {
    for (const m of ['album has already been added', 'Lidarr 404 not found'])
      expect(isImageFetchError(m)).toBe(false)
  })
})
