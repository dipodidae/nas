// Builds a self-contained, copy-paste prompt for an EXTERNAL LLM (Claude,
// ChatGPT, …) to research albums for a spec and return them in the exact
// "Artist - Album" shape the album box round-trips. Pure string templating —
// no network, no API key — so the Discover tab's prompt builder works even
// when the in-app OpenAI integration is disabled.
//
// Kept in sync (in spirit) with server/utils/openai.ts SYSTEM_PROMPT: the base
// curator rules are the same; the difference is the strict line-only output
// format (the API path uses a JSON schema instead) and the optional "flavor"
// lines the UI toggles bolt on.

export interface PromptFlavors {
  // Research thoroughly, go past the obvious, aim for the full count.
  deepDive?: boolean
  // Bias toward obscure / underrated / overlooked over greatest-hits.
  hiddenGems?: boolean
  // Allow EPs, live albums, and compilations (otherwise studio-only).
  includeNonStudio?: boolean
}

export interface ExternalPromptOptions {
  spec: string
  count: number
  flavors: PromptFlavors
}

// Each flavor toggle, in display order, for the UI to render as checkboxes.
export const PROMPT_FLAVORS: { key: keyof PromptFlavors, label: string, hint: string }[] = [
  { key: 'deepDive', label: 'Deep dive', hint: 'exhaustive — beyond the obvious, deep cuts, aim for the full count' },
  { key: 'hiddenGems', label: 'Hidden gems', hint: 'favor obscure / underrated / overlooked over greatest-hits' },
  { key: 'includeNonStudio', label: 'EPs / live / comps', hint: 'allow EPs, live albums, and compilations' },
]

export function buildExternalPrompt(o: ExternalPromptOptions): string {
  const spec = o.spec.trim()
  const lines: string[] = [
    'You are a meticulous music curator with deep knowledge of recorded music and',
    'how releases are catalogued on MusicBrainz (the database Lidarr uses).',
    '',
    `Find real, officially released albums that genuinely match this request:`,
    `"""`,
    spec,
    `"""`,
    '',
    `Return up to ${o.count} albums, best / most representative first.`,
    '',
    'Rules:',
    '- Only real, officially released albums. Never invent, guess, or hallucinate a',
    '  title — if you are not confident an album is real, omit it.',
    '- Use the canonical artist name and the exact studio album title as catalogued',
    '  on MusicBrainz. Do NOT append "(Deluxe)", "(Remastered)", year suffixes, or',
    '  edition tags unless that text is genuinely part of the title.',
    '- No duplicates.',
    '- If fewer genuine matches exist than requested, return fewer rather than',
    '  padding with weak or fabricated entries.',
  ]

  if (o.flavors.includeNonStudio)
    lines.push('- EPs, live albums, and compilations are allowed when they genuinely fit.')
  else
    lines.push('- Prefer full studio albums; exclude live albums, EPs, and compilations unless the request explicitly asks for them.')

  if (o.flavors.deepDive) {
    lines.push('- Do a deep, exhaustive dive: research thoroughly, go well beyond the obvious')
    lines.push('  hits, surface deep cuts and lesser-known releases that truly fit, and try to')
    lines.push('  reach the full requested count.')
  }

  if (o.flavors.hiddenGems)
    lines.push('- Favor obscure, underrated, cult, and overlooked records over the canonical greatest-hits everyone already knows.')

  lines.push(
    '',
    'Output format (important):',
    '- Output ONLY the list, one per line, as exactly: Artist - Album',
    '- No numbering, no notes, no years, no extra text, no markdown, no commentary.',
    '- Canonical names only, so the list can be pasted straight into Lidarr.',
    '',
    'Example:',
    'Joy Division - Closer',
    'The Cure - Pornography',
  )

  return lines.join('\n')
}
