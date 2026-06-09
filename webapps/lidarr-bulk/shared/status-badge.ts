import type { ItemStatus } from './types'

export type BadgeColor = 'neutral' | 'primary' | 'success' | 'warning' | 'error'

export interface StatusBadge {
  label: string
  color: BadgeColor
  icon?: string
}

// Single source of truth for how a job item's status renders. Replaces the
// inline badge() function and the .status CSS classes from the old index.vue.
export function statusBadge(status: ItemStatus): StatusBadge {
  switch (status) {
    case 'done': return { label: 'added', color: 'success', icon: 'i-lucide-check' }
    case 'nudged': return { label: 'nudged', color: 'success', icon: 'i-lucide-refresh-cw' }
    case 'already-added': return { label: 'already in lidarr', color: 'success', icon: 'i-lucide-check' }
    case 'would-add': return { label: 'would add (dry-run)', color: 'primary', icon: 'i-lucide-diamond' }
    case 'not-found': return { label: 'not found', color: 'error', icon: 'i-lucide-x' }
    case 'error': return { label: 'error', color: 'error', icon: 'i-lucide-triangle-alert' }
    case 'skipped': return { label: 'skipped', color: 'neutral' }
    case 'needs-choice': return { label: 'pick one', color: 'warning', icon: 'i-lucide-circle-help' }
    case 'searching': return { label: 'searching…', color: 'neutral' }
    case 'adding': return { label: 'adding…', color: 'neutral' }
    case 'searching-on-lidarr': return { label: 'queued', color: 'neutral' }
    case 'matched': return { label: 'matched', color: 'primary' }
    case 'parsed': return { label: 'parsed', color: 'neutral' }
    default: return { label: status, color: 'neutral' }
  }
}
