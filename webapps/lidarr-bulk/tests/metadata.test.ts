import { describe, expect, it } from 'vitest'
import { rankVaAlbums, VARIOUS_ARTISTS_MBID } from '../server/utils/metadata'

const VA = VARIOUS_ARTISTS_MBID
function entry(id: string, title: string, artistid: string, releasedate?: string) {
  return { album: { id, title, artistid, releasedate } }
}

describe('rankVaAlbums', () => {
  it('keeps only VA-credited albums and ranks by title similarity', () => {
    const out = rankVaAlbums([
      entry('m1', 'Pulp Fiction', VA, '1994-09-26'),
      entry('m2', 'Pulp Fiction Karaoke', VA, '2005'),
      entry('m3', 'Pulp Fiction', 'real-artist-id', '1994'), // not VA → dropped
      { album: null },
    ], 'Pulp Fiction')
    expect(out.map(m => m.mbid)).toEqual(['m1', 'm2'])
    expect(out[0]).toMatchObject({ mbid: 'm1', title: 'Pulp Fiction', year: 1994 })
  })

  it('uses release year as a tiebreak between equally-titled comps', () => {
    const out = rankVaAlbums([
      entry('old', 'Greatest Hits', VA, '1980'),
      entry('new', 'Greatest Hits', VA, '2014'),
    ], 'Greatest Hits', 2014)
    expect(out[0].mbid).toBe('new')
  })

  it('dedupes by mbid and respects the limit', () => {
    const out = rankVaAlbums([entry('m1', 'X', VA), entry('m1', 'X', VA), entry('m2', 'X', VA)], 'X', undefined, 1)
    expect(out).toHaveLength(1)
  })
})
