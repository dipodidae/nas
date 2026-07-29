// Writing-system awareness. Pure functions, no I/O.
//
// Why this exists: Spotify hands us a *romanized* artist name alongside a
// *native-script* album title — "Basta" + "На заре", "AIGEL" + "Пыяла". Joining
// those into one lookup term poisons it, because Lidarr's search (and the
// MusicBrainz index behind it) effectively discards the non-Latin half:
//
//   "Basta На заре"  → 10 hits, all Swedish "Bästa" compilations
//   "На заре"        → the real Баста // На заре is in the list
//   "Баста На заре"  → the real one at position 0
//
// So callers need to (a) detect the mismatch to pick better query terms, and
// (b) compare names across scripts, since "Баста" and "Basta" are the same
// artist but score 0 similarity as raw strings.
import { normKey, similarity } from './text'

export type Script = 'latin' | 'cyrillic' | 'other' | 'none'

// Primary BGN/PCGN-style readings. Every source key is a single code point, so
// substitution is order-independent and we can walk the string char by char.
const PRIMARY: Record<string, string> = {
  // Russian
  а: 'a',
  б: 'b',
  в: 'v',
  г: 'g',
  д: 'd',
  е: 'e',
  ё: 'yo',
  ж: 'zh',
  з: 'z',
  и: 'i',
  й: 'y',
  к: 'k',
  л: 'l',
  м: 'm',
  н: 'n',
  о: 'o',
  п: 'p',
  р: 'r',
  с: 's',
  т: 't',
  у: 'u',
  ф: 'f',
  х: 'kh',
  ц: 'ts',
  ч: 'ch',
  ш: 'sh',
  щ: 'shch',
  ъ: '',
  ы: 'y',
  ь: '',
  э: 'e',
  ю: 'yu',
  я: 'ya',
  // Ukrainian / Belarusian
  і: 'i',
  ї: 'yi',
  є: 'ye',
  ґ: 'g',
  ў: 'u',
  // Serbian / Macedonian
  ј: 'j',
  љ: 'lj',
  њ: 'nj',
  ћ: 'c',
  ђ: 'dj',
  џ: 'dz',
  ѕ: 'dz',
  ѓ: 'g',
  ќ: 'k',
}

// Letters whose romanization genuinely varies between systems, in the order we
// are willing to spend variants on — ц (ts vs the Polish/Czech-style c) diverges
// most often in practice, е (e vs ye) least. Spotify is inconsistent about all
// of them, which is why we score best-of instead of picking one scheme.
const AMBIGUOUS: [string, string[]][] = [
  ['ц', ['ts', 'c']],
  ['ж', ['zh', 'j']],
  ['я', ['ya', 'ia']],
  ['й', ['y', 'i']],
  ['щ', ['shch', 'sch']],
  ['х', ['kh', 'h']],
  ['ю', ['yu', 'iu']],
  ['ё', ['yo', 'e']],
  ['е', ['e', 'ye']],
]

// Combinatorial ceiling. A title dense in ambiguous letters would otherwise
// generate 2^9 readings, each costing a Levenshtein pass per comparison.
const MAX_VARIANTS = 8

const HAS_CYRILLIC = /\p{Script=Cyrillic}/u

function countLetters(s: string): { latin: number, cyrillic: number, other: number } {
  let latin = 0
  let cyrillic = 0
  let other = 0
  for (const ch of s) {
    if (!/\p{L}/u.test(ch))
      continue
    if (/\p{Script=Latin}/u.test(ch))
      latin++
    else if (/\p{Script=Cyrillic}/u.test(ch))
      cyrillic++
    else
      other++
  }
  return { latin, cyrillic, other }
}

// Which writing system a string is *mostly* in. Digits and punctuation don't
// vote, so "2000$" and "" are both 'none' — callers must treat 'none' as "no
// evidence" rather than as a script that can mismatch.
export function dominantScript(s: string | undefined): Script {
  const { latin, cyrillic, other } = countLetters(s ?? '')
  if (latin === 0 && cyrillic === 0 && other === 0)
    return 'none'
  if (latin >= cyrillic && latin >= other)
    return 'latin'
  if (cyrillic >= other)
    return 'cyrillic'
  return 'other'
}

// True when artist and title are written in different systems — the Spotify
// signature that makes a combined "artist title" lookup term unusable. Requires
// evidence on both sides: a title like "3000" carries no script information, so
// it is not a mismatch, just unknown.
export function isMixedScript(artist: string | undefined, title: string | undefined): boolean {
  const a = dominantScript(artist)
  const t = dominantScript(title)
  if (a === 'none' || t === 'none')
    return false
  return a !== t
}

// Choose one reading per ambiguous letter present in the source, breadth-first
// over AMBIGUOUS priority, stopping before the ceiling is breached. Letters we
// stop short of keep their PRIMARY reading, so the returned list always leads
// with the full primary romanization.
function readingChoices(src: string): Record<string, string>[] {
  let combos: Record<string, string>[] = [{}]
  for (const [ch, readings] of AMBIGUOUS) {
    if (!src.includes(ch))
      continue
    const next: Record<string, string>[] = []
    for (const combo of combos) {
      for (const r of readings)
        next.push({ ...combo, [ch]: r })
    }
    if (next.length > MAX_VARIANTS)
      break
    combos = next
  }
  return combos
}

function transliterate(src: string, overrides: Record<string, string>): string {
  let out = ''
  for (const ch of src)
    out += overrides[ch] ?? PRIMARY[ch] ?? ch
  return out
}

// Comparison keys for a name: the plain folded form for Latin input, or every
// plausible romanization for Cyrillic input (primary reading first). Always
// returns at least one entry, deduped.
export function romanizeVariants(s: string | undefined): string[] {
  // NFC first: romanization is a code-point table, so a decomposed й (и + ◌̆)
  // must be recomposed before we look it up, and normKey's NFKD pass must run
  // strictly afterwards.
  const src = (s ?? '').normalize('NFC').toLowerCase()
  if (!HAS_CYRILLIC.test(src))
    return [normKey(src)]
  const out: string[] = []
  for (const overrides of readingChoices(src)) {
    const key = normKey(transliterate(src, overrides))
    if (key && !out.includes(key))
      out.push(key)
  }
  return out.length > 0 ? out : [normKey(src)]
}

// Best similarity achievable between two names once both sides are folded to a
// common script. Symmetric. For same-script input this degrades to plain
// normKey similarity, so it is safe to use as a drop-in comparison everywhere.
export function bestCrossScriptSimilarity(a: string | undefined, b: string | undefined): number {
  const av = romanizeVariants(a)
  const bv = romanizeVariants(b)
  let best = 0
  for (const x of av) {
    for (const y of bv) {
      const s = similarity(x, y)
      if (s > best)
        best = s
      if (best === 1)
        return 1
    }
  }
  return best
}
