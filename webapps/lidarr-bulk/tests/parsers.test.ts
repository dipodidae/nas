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
  it('flags ambiguous lines as needsReview instead of guessing', () => {
    const r = parseAlbums('justonething')
    expect(r[0]).toMatchObject({ needsReview: true })
  })
  it('dedupes by (artist,title)', () => {
    const r = parseAlbums('Adele - 30\nadele - 30')
    expect(r.length).toBe(1)
  })
})
