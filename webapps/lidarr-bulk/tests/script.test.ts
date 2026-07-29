import { describe, expect, it } from 'vitest'
import { bestCrossScriptSimilarity, dominantScript, isMixedScript, romanizeVariants } from '../server/utils/script'

describe('dominantScript', () => {
  it('classifies pure scripts', () => {
    expect(dominantScript('Iron Maiden')).toBe('latin')
    expect(dominantScript('Баста')).toBe('cyrillic')
    expect(dominantScript('ピンク・フロイド')).toBe('other')
  })

  it('returns none when there are no letters at all', () => {
    expect(dominantScript('2000$')).toBe('none')
    expect(dominantScript('  ')).toBe('none')
    expect(dominantScript('')).toBe('none')
  })

  it('picks the majority script in mixed strings', () => {
    // "7Б" — one Cyrillic letter, no Latin letters.
    expect(dominantScript('7Б')).toBe('cyrillic')
    // Latin band name with one stray Cyrillic char stays latin.
    expect(dominantScript('Kinoа')).toBe('latin')
  })

  it('ignores digits and punctuation when deciding', () => {
    expect(dominantScript('25 Кадр')).toBe('cyrillic')
    expect(dominantScript('808 Squadliners Beatz')).toBe('latin')
  })
})

describe('isMixedScript', () => {
  it('flags the Spotify signature: romanized artist + native-script title', () => {
    expect(isMixedScript('Basta', 'На заре')).toBe(true)
    expect(isMixedScript('AIGEL', 'Пыяла')).toBe(true)
    expect(isMixedScript('Smyslovye Gallyutsinatsii', '3000')).toBe(false) // title has no letters
  })

  it('does not flag same-script pairs', () => {
    expect(isMixedScript('Iron Maiden', 'Powerslave')).toBe(false)
    expect(isMixedScript('Владимир Клявин', 'Бездна')).toBe(false)
  })

  it('is false when either side has no letters to judge', () => {
    expect(isMixedScript('', 'На заре')).toBe(false)
    expect(isMixedScript(undefined, 'На заре')).toBe(false)
    expect(isMixedScript('Basta', '')).toBe(false)
  })
})

describe('romanizeVariants', () => {
  it('leaves Latin input untouched as a single variant', () => {
    expect(romanizeVariants('Iron Maiden')).toEqual(['iron maiden'])
  })

  it('romanizes Cyrillic with the primary BGN/PCGN reading first', () => {
    expect(romanizeVariants('Баста')[0]).toBe('basta')
    expect(romanizeVariants('Сектор Газа')[0]).toBe('sektor gaza')
  })

  it('emits alternate readings for ambiguous letters', () => {
    // ц → ts (primary) and c (Polish/Czech-style, which Spotify often uses)
    expect(romanizeVariants('Комбинация')).toContain('kombinatsiya')
    expect(romanizeVariants('Комбинация')).toContain('kombinaciya')
    // й → y and i
    expect(romanizeVariants('Ласковый Май')).toContain('laskovyy may')
    // ж → zh and j
    expect(romanizeVariants('Мираж')).toContain('mirazh')
    expect(romanizeVariants('Мираж')).toContain('miraj')
  })

  it('drops the soft and hard signs', () => {
    expect(romanizeVariants('Альянс')).toContain('alyans')
  })

  it('caps the variant count so the fold stays cheap', () => {
    // A title dense in ambiguous letters must not explode combinatorially.
    const v = romanizeVariants('Цыцый жяця ЩЁЙ ця жий')
    expect(v.length).toBeLessThanOrEqual(8)
    expect(new Set(v).size).toBe(v.length) // deduped
  })

  it('handles Ukrainian / Belarusian / Serbian extras', () => {
    expect(romanizeVariants('Київ')[0]).toContain('ki')
    expect(romanizeVariants('Ђорђе')).toBeTruthy()
  })
})

// The 20 real artist pairs sampled from the user's failing Spotify playlists:
// Spotify's romanized name on the left, MusicBrainz's native name on the right.
const REAL_PAIRS: [string, string][] = [
  ['Basta', 'Баста'],
  ['AIGEL', 'Аигел'],
  ['Alyans', 'Альянс'],
  ['Kino', 'Кино'],
  ['Forum', 'Форум'],
  ['Igor Talkov', 'Игорь Тальков'],
  ['Sektor Gaza', 'Сектор Газа'],
  ['Natalya Vetlitskaya', 'Наталья Ветлицкая'],
  ['Rok-Ostrova', 'Рок-Острова'],
  ['Tantsy Minus', 'Танцы Минус'],
  ['Aleksander Serov', 'Александр Серов'],
  ['Oleg Anofriyev', 'Олег Анофриев'],
  ['7B', '7Б'],
  ['Smyslovye Gallyutsinatsii', 'Смысловые Галлюцинации'],
  ['Nautilus Pompilius', 'Наутилус Помпилиус'],
  ['Vladimir Klyavin', 'Владимир Клявин'],
  ['Kombinaciya', 'Комбинация'],
  ['Laskovyy May', 'Ласковый Май'],
  ['Mirage', 'Мираж'],
  ['Splean', 'Сплин'],
]

describe('bestCrossScriptSimilarity on real Spotify↔MusicBrainz pairs', () => {
  // Мираж→Mirage and Сплин→Splean are genuine MusicBrainz *aliases*, not
  // transliterations — no romanization table can reach them, so they are
  // expected to fall short here and are recovered by the alias path instead.
  const ALIAS_ONLY = new Set(['Mirage', 'Splean'])

  for (const [latin, cyrillic] of REAL_PAIRS) {
    const aliasOnly = ALIAS_ONLY.has(latin)
    it(`${aliasOnly ? 'falls short on' : 'matches'} ${latin} ↔ ${cyrillic}`, () => {
      const score = bestCrossScriptSimilarity(latin, cyrillic)
      if (aliasOnly)
        expect(score).toBeLessThan(0.85)
      else
        expect(score).toBeGreaterThanOrEqual(0.85)
    })
  }

  it('does not create false positives between unrelated names', () => {
    expect(bestCrossScriptSimilarity('Basta', 'Мираж')).toBeLessThan(0.6)
    expect(bestCrossScriptSimilarity('Kino', 'Сектор Газа')).toBeLessThan(0.6)
    // The Swedish "Bästa" albums that poisoned the original query must not
    // suddenly look like the Cyrillic Баста beyond what plain fuzzy already did.
    expect(bestCrossScriptSimilarity('Bästa', 'Баста')).toBeGreaterThanOrEqual(0.8)
  })

  it('is symmetric', () => {
    expect(bestCrossScriptSimilarity('Баста', 'Basta')).toBe(bestCrossScriptSimilarity('Basta', 'Баста'))
  })

  it('matches native-script titles to themselves exactly', () => {
    expect(bestCrossScriptSimilarity('На заре', 'На заре')).toBe(1)
    expect(bestCrossScriptSimilarity('Пыяла', 'Пыяла')).toBe(1)
  })
})
