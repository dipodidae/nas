// Pure parsers — no Nuxt / Node deps. Tested in tests/parsers.test.ts.
import type { ParsedItem } from '~~/shared/types'

const QUOTES = /^['"`“”‘’]+|['"`“”‘’]+$/g
const DASHES = /\s+[–—―-]\s+/ // en/em/hyphen between two parts, requires surrounding whitespace
const BY_SPLIT = /\s+by\s+/i
const PIPE_SPLIT = /\s*\|\s*/

// Split a blob into raw lines for the *album* parser. Album titles can contain
// commas, so when quotes are present we only split on newlines (CSV-aware).
function rawLinesAlbums(blob: string): string[] {
  const text = blob.replace(/\r\n?/g, '\n')
  const hasQuoted = /["“”]/.test(text)
  const splitter = hasQuoted ? /\n+/ : /[\n,;\t]+/
  return text.split(splitter).map(s => s.trim()).filter(Boolean)
}

// Artist parser splits aggressively on any common separator. Artists rarely
// contain commas; the rare exceptions can be edited by the user post-parse.
function rawLinesArtists(blob: string): string[] {
  const text = blob.replace(/\r\n?/g, '\n')
  return text.split(/[\n,;\t]+/).map(s => s.trim()).filter(Boolean)
}

function clean(s: string): string {
  return s.replace(QUOTES, '').trim()
}

function dedupe(items: string[]): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const v of items) {
    const key = v.toLowerCase()
    if (seen.has(key))
      continue
    seen.add(key)
    out.push(v)
  }
  return out
}

// --- Artist parser -----------------------------------------------------------
// NOTE: we deliberately do NOT split on whitespace — multi-word band names
// ("Satanic Warmaster", "Nine Inch Nails", "Pink Floyd") are the common case.
// If you paste a single line with no separator, that's one artist.
export function parseArtists(blob: string): ParsedItem[] {
  const lines = rawLinesArtists(blob)
  const cleaned = lines.map(clean).filter(Boolean)
  return dedupe(cleaned).map(name => ({
    raw: name,
    kind: 'artist' as const,
  }))
}

// --- Album parser ------------------------------------------------------------
// Recognized shapes (per line, after trimming/quote-stripping):
//   "Artist - Album"       (hyphen, en-dash, em-dash)
//   "Album by Artist"
//   "Artist | Album"
//   '"Artist","Album"'     (CSV-ish, with or without header)
//   bare line              -> needs review
//
// We don't try to be clever about dash usage inside titles. If a line has
// multiple dash-separators, we take the first split.
function parseCsvPair(line: string): { a: string, b: string } | undefined {
  // Minimal CSV: two quoted or unquoted fields separated by a comma.
  const m = line.match(/^\s*"?([^",]+(?:,[^",]+)*)"?\s*,\s*"?([^",]+(?:,[^",]+)*)"?\s*$/)
  if (!m)
    return
  return { a: clean(m[1]!), b: clean(m[2]!) }
}

function detectHeader(line: string): boolean {
  // "artist,album" or "Artist Name,Album Name" headers — skip them.
  const lower = line.toLowerCase()
  return /^\s*"?artist"?\s*,\s*"?(album|title|release)"?\s*$/.test(lower)
}

export function parseAlbums(blob: string): ParsedItem[] {
  const lines = rawLinesAlbums(blob)
  const out: ParsedItem[] = []
  for (const rawLine of lines) {
    const line = clean(rawLine)
    if (!line)
      continue
    if (detectHeader(line))
      continue

    // 1) CSV pair
    const csv = parseCsvPair(line)
    if (csv) {
      out.push({ raw: rawLine, kind: 'album', artist: csv.a, title: csv.b })
      continue
    }

    // 2) Pipe-separated
    if (PIPE_SPLIT.test(line)) {
      const [a, b] = line.split(PIPE_SPLIT, 2)
      if (a && b) {
        out.push({ raw: rawLine, kind: 'album', artist: clean(a), title: clean(b) })
        continue
      }
    }

    // 3) "Album by Artist"
    if (BY_SPLIT.test(line)) {
      const [b, a] = line.split(BY_SPLIT)
      if (a && b) {
        out.push({ raw: rawLine, kind: 'album', artist: clean(a), title: clean(b) })
        continue
      }
    }

    // 4) "Artist - Album" / en-dash / em-dash
    if (DASHES.test(line)) {
      const idx = line.search(DASHES)
      const a = line.slice(0, idx)
      const b = line.slice(idx).replace(DASHES, '')
      if (a && b) {
        out.push({ raw: rawLine, kind: 'album', artist: clean(a), title: clean(b) })
        continue
      }
    }

    // 5) Couldn't split — bucket as needs-review with raw text.
    out.push({ raw: rawLine, kind: 'album', needsReview: true })
  }

  // Dedupe by (artist|title) when both present, else by raw line.
  const seen = new Set<string>()
  return out.filter((item) => {
    const key = item.artist && item.title
      ? `${item.artist.toLowerCase()}|${item.title.toLowerCase()}`
      : item.raw.toLowerCase()
    if (seen.has(key))
      return false
    seen.add(key)
    return true
  })
}
