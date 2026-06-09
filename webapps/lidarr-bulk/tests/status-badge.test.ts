import type { ItemStatus } from '../shared/types'
import { describe, expect, it } from 'vitest'
import { statusBadge } from '../shared/status-badge'

const ALL: ItemStatus[] = [
  'parsed', 'searching', 'needs-choice', 'matched', 'adding',
  'searching-on-lidarr', 'done', 'nudged', 'already-added',
  'would-add', 'not-found', 'error', 'skipped',
]

describe('statusBadge', () => {
  it('maps every ItemStatus to a label and color', () => {
    for (const s of ALL) {
      const b = statusBadge(s)
      expect(b.label.length).toBeGreaterThan(0)
      expect(b.color.length).toBeGreaterThan(0)
    }
  })

  it('uses success color for added states', () => {
    expect(statusBadge('done').color).toBe('success')
    expect(statusBadge('nudged').color).toBe('success')
    expect(statusBadge('already-added').color).toBe('success')
  })

  it('uses error color for failures', () => {
    expect(statusBadge('error').color).toBe('error')
    expect(statusBadge('not-found').color).toBe('error')
  })

  it('uses warning color for needs-choice', () => {
    expect(statusBadge('needs-choice').color).toBe('warning')
  })
})
