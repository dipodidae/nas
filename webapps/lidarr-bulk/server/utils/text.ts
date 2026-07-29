// Lowest-level string primitives. No imports from other utils — this is the
// bottom of the dependency chain (text → script → matching), so both the
// romanization layer and the matcher can share these without a cycle.

// Aggressive comparison key. NFKD-decomposes Unicode (ö → o + combining ¨),
// strips the combining marks, normalizes smart quotes / em-dashes / ellipsis
// to ASCII equivalents, then strips everything that isn't a letter, digit, or
// space. So "¡Comprendido!… …World Ending" and "Comprendido! ...World Ending"
// both reduce to "comprendido world ending".
//
// NOTE: the NFKD pass is why romanization must run *before* this, never after —
// NFKD decomposes й into и + combining breve and then the mark strip leaves a
// bare и, silently turning "Май" into "mai" instead of "may".
export function normKey(s: string | undefined): string {
  return (s ?? '')
    .normalize('NFKD')
    .toLowerCase()
    .replace(/\p{M}/gu, '')
    .replace(/[‘’‚‛′]/g, '\'')
    .replace(/[“”„‟″]/g, '"')
    .replace(/[–—−]/g, '-')
    .replace(/…/g, '...')
    .replace(/[^\p{L}\p{N}\s]/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

export function levenshtein(a: string, b: string): number {
  if (a === b)
    return 0
  if (!a.length)
    return b.length
  if (!b.length)
    return a.length
  let prev = Array.from({ length: b.length + 1 }, (_, i) => i)
  for (let i = 1; i <= a.length; i++) {
    const curr: number[] = [i]
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1
      curr.push(Math.min(prev[j]! + 1, curr[j - 1]! + 1, prev[j - 1]! + cost))
    }
    prev = curr
  }
  return prev[b.length]!
}

export function similarity(a: string, b: string): number {
  if (a === b)
    return 1
  if (!a.length || !b.length)
    return 0
  return 1 - levenshtein(a, b) / Math.max(a.length, b.length)
}
