import { describe, expect, it } from 'vitest'
import { parseAlbums, parseArtists } from '../server/utils/parsers'

describe('parseArtists', () => {
  it('handles newline-separated', () => {
    expect(parseArtists('Adele\nBeyoncé\nSade').map(p => p.raw))
      .toEqual(['Adele', 'Beyoncé', 'Sade'])
  })
  it('handles comma/semicolon/tab/space', () => {
    expect(parseArtists('Adele, Beyoncé; Sade\tBjörk').map(p => p.raw))
      .toEqual(['Adele', 'Beyoncé', 'Sade', 'Björk'])
  })
  it('strips quotes and dedupes case-insensitively', () => {
    expect(parseArtists('"Adele"\n adele \nBeyoncé').map(p => p.raw))
      .toEqual(['Adele', 'Beyoncé'])
  })
  it('preserves multi-word band names (no whitespace splitting)', () => {
    expect(parseArtists('Satanic Warmaster').map(p => p.raw))
      .toEqual(['Satanic Warmaster'])
    expect(parseArtists('Nine Inch Nails\nPink Floyd').map(p => p.raw))
      .toEqual(['Nine Inch Nails', 'Pink Floyd'])
  })
})

describe('parseAlbums', () => {
  it('parses Artist - Album', () => {
    const r = parseAlbums('Adele - 30\nBeyoncé - Lemonade')
    expect(r).toEqual([
      { raw: 'Adele - 30', kind: 'album', artist: 'Adele', title: '30' },
      { raw: 'Beyoncé - Lemonade', kind: 'album', artist: 'Beyoncé', title: 'Lemonade' },
    ])
  })
  it('parses en-dash and em-dash', () => {
    const r = parseAlbums('Adele – 30\nBeyoncé — Lemonade')
    expect(r.map(x => ({ a: x.artist, t: x.title }))).toEqual([
      { a: 'Adele', t: '30' },
      { a: 'Beyoncé', t: 'Lemonade' },
    ])
  })
  it('parses "Album by Artist"', () => {
    const r = parseAlbums('30 by Adele')
    expect(r[0]).toMatchObject({ artist: 'Adele', title: '30' })
  })
  it('parses pipe-separated', () => {
    const r = parseAlbums('Adele | 30')
    expect(r[0]).toMatchObject({ artist: 'Adele', title: '30' })
  })
  it('parses CSV with header skipped', () => {
    const r = parseAlbums('Artist,Album\n"Adele","30"\n"Beyoncé","Lemonade"')
    expect(r.length).toBe(2)
    expect(r[0]).toMatchObject({ artist: 'Adele', title: '30' })
  })
  it('keeps a comma inside an album title on one row', () => {
    // Regression: commas used to split album blobs, so this became two bogus
    // rows ("… — Городок" and "что я выдумал"), each failing its own lookup.
    const r = parseAlbums('Татьяна Никитина и Сергей Никитин — Городок, что я выдумал')
    expect(r).toHaveLength(1)
    expect(r[0]).toMatchObject({
      artist: 'Татьяна Никитина и Сергей Никитин',
      title: 'Городок, что я выдумал',
    })
  })

  it('keeps a comma inside an English album title on one row', () => {
    const r = parseAlbums('Bright Eyes - I\'m Wide Awake, It\'s Morning')
    expect(r).toHaveLength(1)
    expect(r[0]).toMatchObject({ artist: 'Bright Eyes', title: 'I\'m Wide Awake, It\'s Morning' })
  })

  it('keeps a comma inside a band name on one row', () => {
    const r = parseAlbums('Emerson, Lake & Palmer - Trilogy')
    expect(r).toHaveLength(1)
    expect(r[0]).toMatchObject({ artist: 'Emerson, Lake & Palmer', title: 'Trilogy' })
  })

  it('still splits unquoted CSV when nothing else looks like a separator', () => {
    const r = parseAlbums('Adele,30')
    expect(r[0]).toMatchObject({ artist: 'Adele', title: '30' })
  })

  it('still splits on semicolons and newlines', () => {
    const r = parseAlbums('Adele - 30;Beyoncé - Lemonade\nSade - Love Deluxe')
    expect(r).toHaveLength(3)
  })

  it('flags ambiguous lines as needsReview instead of guessing', () => {
    const r = parseAlbums('justonething')
    expect(r[0]).toMatchObject({ needsReview: true })
  })
  it('dedupes by (artist,title)', () => {
    const r = parseAlbums('Adele - 30\nadele - 30')
    expect(r.length).toBe(1)
  })
  it('flags Various Artists rows from free text', () => {
    const out = parseAlbums('Various Artists - Pulp Fiction\nvarious - Trainspotting\nReal Band - Their LP')
    const byTitle = (t: string) => out.find(i => i.title === t)
    expect(byTitle('Pulp Fiction')?.variousArtists).toBe(true)
    expect(byTitle('Trainspotting')?.variousArtists).toBe(true)
    expect(byTitle('Their LP')?.variousArtists).toBeUndefined()
  })
})
