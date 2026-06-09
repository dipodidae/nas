// GPT-backed album discovery. Turns a free-text prompt ("the best 80s coldwave
// albums") into a clean, deduped list of real Artist/Album pairs ready to feed
// the existing album job flow. Pure shaping helpers live alongside the network
// call so they can be unit-tested without hitting the API.
import type { ParsedItem } from '~~/shared/types'

export interface RawAlbum {
  artist?: unknown
  album?: unknown
  year?: unknown
}

// Guardrails for how many albums we ever ask for / return in one shot. Keeping
// the ceiling modest protects the Lidarr lookup pipeline (each item is a
// MusicBrainz round-trip) and keeps GPT focused on quality over quantity.
export const AI_MIN_COUNT = 1
export const AI_MAX_COUNT = 50
export const AI_DEFAULT_COUNT = 25
export const AI_PROMPT_MAX_CHARS = 2000

export function clampCount(n: number | undefined): number {
  if (!Number.isFinite(n) || n === undefined)
    return AI_DEFAULT_COUNT
  return Math.min(AI_MAX_COUNT, Math.max(AI_MIN_COUNT, Math.trunc(n)))
}

// Turn whatever GPT returned into trustworthy ParsedItems:
//   - drop rows missing artist or album
//   - trim, collapse whitespace
//   - dedupe case-insensitively on artist|album
//   - cap at `count` (GPT is told the limit, but we never trust it)
export function normalizeAlbums(raw: RawAlbum[], count: number): ParsedItem[] {
  const seen = new Set<string>()
  const out: ParsedItem[] = []
  for (const r of raw) {
    const artist = typeof r.artist === 'string' ? r.artist.replace(/\s+/g, ' ').trim() : ''
    const title = typeof r.album === 'string' ? r.album.replace(/\s+/g, ' ').trim() : ''
    if (!artist || !title)
      continue
    const key = `${artist.toLowerCase()}|${title.toLowerCase()}`
    if (seen.has(key))
      continue
    seen.add(key)
    out.push({ raw: `${artist} - ${title}`, kind: 'album', artist, title })
    if (out.length >= count)
      break
  }
  return out
}

const SYSTEM_PROMPT = [
  'You are a meticulous music curator with deep knowledge of recorded music and',
  'how releases are catalogued on MusicBrainz (the database Lidarr uses).',
  'Given a description, return a list of real, officially released albums that',
  'genuinely match it.',
  '',
  'Hard rules:',
  '- Only return albums and artists that actually exist. Never invent or guess a',
  '  title. If you are not confident an album is real, omit it.',
  '- Use the canonical artist name and the exact studio album title as it appears',
  '  on MusicBrainz. Do NOT append "(Deluxe)", "(Remastered)", year suffixes, or',
  '  edition tags unless that text is part of the canonical title.',
  '- Prefer full studio albums. Only include live albums, EPs, or compilations if',
  '  the request explicitly asks for them.',
  '- No duplicates. Order the list best / most representative first.',
  '- If fewer genuine matches exist than requested, return fewer rather than',
  '  padding with weak or fabricated entries.',
].join('\n')

const RESPONSE_FORMAT = {
  type: 'json_schema',
  json_schema: {
    name: 'album_list',
    strict: true,
    schema: {
      type: 'object',
      additionalProperties: false,
      properties: {
        albums: {
          type: 'array',
          items: {
            type: 'object',
            additionalProperties: false,
            properties: {
              artist: { type: 'string', description: 'Canonical artist name' },
              album: { type: 'string', description: 'Canonical studio album title' },
              year: { type: ['integer', 'null'], description: 'Original release year if known' },
            },
            required: ['artist', 'album', 'year'],
          },
        },
      },
      required: ['albums'],
    },
  },
} as const

export interface SuggestOptions {
  apiKey: string
  model: string
  prompt: string
  count: number
}

// Calls OpenAI chat completions with a strict JSON schema so the response is
// always shaped { albums: [...] }. Throws on transport / API errors; the caller
// maps that to an HTTP status.
export async function suggestAlbums(opts: SuggestOptions): Promise<ParsedItem[]> {
  const userPrompt = [
    `Request: ${opts.prompt.trim()}`,
    '',
    `Return up to ${opts.count} albums.`,
  ].join('\n')

  const res = await fetch('https://api.openai.com/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${opts.apiKey}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      model: opts.model,
      temperature: 0.4,
      messages: [
        { role: 'system', content: SYSTEM_PROMPT },
        { role: 'user', content: userPrompt },
      ],
      response_format: RESPONSE_FORMAT,
    }),
  })

  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`OpenAI ${res.status}: ${body.slice(0, 400) || res.statusText}`)
  }

  const data = await res.json() as {
    choices?: { message?: { content?: string, refusal?: string } }[]
  }
  const msg = data.choices?.[0]?.message
  if (msg?.refusal)
    throw new Error(`OpenAI refused: ${msg.refusal}`)
  const content = msg?.content
  if (!content)
    throw new Error('OpenAI returned an empty response')

  let parsed: { albums?: RawAlbum[] }
  try {
    parsed = JSON.parse(content) as { albums?: RawAlbum[] }
  }
  catch {
    throw new Error('OpenAI returned malformed JSON')
  }
  return normalizeAlbums(parsed.albums ?? [], opts.count)
}
