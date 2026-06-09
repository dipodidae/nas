import { describe, expect, it } from 'vitest'
import { buildExternalPrompt } from '../shared/external-prompt'

const base = { spec: 'the best 80s coldwave albums', count: 25, flavors: {} }

describe('buildExternalPrompt', () => {
  it('embeds the spec and the requested count', () => {
    const p = buildExternalPrompt(base)
    expect(p).toContain('the best 80s coldwave albums')
    expect(p).toContain('25')
  })

  it('trims the spec before embedding it', () => {
    const p = buildExternalPrompt({ ...base, spec: '   spacey wave   ' })
    expect(p).toContain('spacey wave')
    expect(p).not.toContain('   spacey wave')
  })

  it('always demands the strict round-trip "Artist - Album" format with no extra text', () => {
    const p = buildExternalPrompt(base)
    expect(p).toContain('Artist - Album')
    expect(p).toMatch(/one per line/i)
    expect(p).toMatch(/no (numbering|notes|extra text|markdown)/i)
    expect(p).toMatch(/canonical/i)
  })

  it('defaults to studio-only when the EP/live/comp flavor is off', () => {
    const p = buildExternalPrompt(base)
    expect(p).toMatch(/studio album/i)
    expect(p).toMatch(/exclude|unless/i)
    expect(p).not.toMatch(/deep[, ]?\s*exhaustive dive/i)
    expect(p).not.toMatch(/underrated/i)
  })

  it('adds deep-dive language only when deepDive is on', () => {
    const off = buildExternalPrompt(base)
    const on = buildExternalPrompt({ ...base, flavors: { deepDive: true } })
    expect(off).not.toMatch(/deep[, ]?\s*exhaustive|deep dive/i)
    expect(on).toMatch(/deep[, ]?\s*exhaustive|deep dive/i)
    expect(on).toMatch(/beyond the obvious|deep cuts/i)
  })

  it('adds hidden-gems language only when hiddenGems is on', () => {
    const off = buildExternalPrompt(base)
    const on = buildExternalPrompt({ ...base, flavors: { hiddenGems: true } })
    expect(off).not.toMatch(/underrated|obscure|overlooked/i)
    expect(on).toMatch(/underrated|obscure|overlooked/i)
  })

  it('opens up EPs/live/comps only when includeNonStudio is on', () => {
    const on = buildExternalPrompt({ ...base, flavors: { includeNonStudio: true } })
    expect(on).toMatch(/EP|live|compilation/i)
    expect(on).toMatch(/allowed|include/i)
    // and it should NOT also tell the model to exclude them
    expect(on).not.toMatch(/exclude (live|eps|compilations)/i)
  })
})
