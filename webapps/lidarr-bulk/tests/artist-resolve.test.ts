import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ArtistIdentity, Discography } from '../server/utils/artist-resolve'
import type { Candidate, LidarrAlbumCandidate, ParsedItem } from '~~/shared/types'

const lookupArtist = vi.fn()
vi.mock('../server/utils/lidarr', () => ({ lookupArtist: (t: string) => lookupArtist(t) }))
vi.mock('../server/utils/env', () => ({
  loadEnv: () => ({ LIDARR_METADATA_URL: 'https://meta.test/api/v0.4' }),
}))

const {
  clearArtistResolveCaches,
  fetchArtistDiscography,
  identityProvesName,
  learnArtistIdentity,
  learnedArtistIdentity,
  parseArtistDoc,
  pickDiscographyAlbum,
  resolveAlbumViaArtist,
  resolveByCandidateAlias,
} = await import('../server/utils/artist-resolve')

function identity(over: Partial<ArtistIdentity> = {}): ArtistIdentity {
  return { mbid: 'a1', name: 'Сплин', sortName: 'Splean', aliases: ['Splin', 'Splean'], ...over }
}

function disco(over: Partial<Discography> = {}): Discography {
  return { ...identity(), albums: [], ...over }
}

function albumCandidate(title: string, artistName: string, foreignArtistId?: string): Candidate {
  return {
    kind: 'album',
    value: {
      foreignAlbumId: `alb-${title}`,
      title,
      artist: { artistName, foreignArtistId },
    } as LidarrAlbumCandidate,
  }
}

// Minimal stub of the metadata backend's artist document.
function docFor(d: Discography): unknown {
  return {
    id: d.mbid,
    artistname: d.name,
    sortname: d.sortName,
    artistaliases: d.aliases,
    Albums: d.albums.map(a => ({
      Id: a.mbid,
      Title: a.title,
      Type: a.type,
      SecondaryTypes: a.secondaryTypes ?? [],
    })),
  }
}

function stubFetch(byMbid: Record<string, unknown>): void {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const mbid = url.split('/').pop() ?? ''
    const body = byMbid[mbid]
    if (body === undefined)
      return { ok: false, status: 404, json: async () => ({}) }
    return { ok: true, status: 200, json: async () => body }
  }))
}

beforeEach(() => {
  clearArtistResolveCaches()
  lookupArtist.mockReset()
  vi.unstubAllGlobals()
})

describe('identityProvesName', () => {
  it('proves an alias that no romanization could reach (Сплин ≡ Splean)', () => {
    expect(identityProvesName(identity(), 'Splean')).toBe(true)
  })

  it('proves via sortname alone (Комбинация ≡ Kombinaciya)', () => {
    const i = identity({ name: 'Комбинация', sortName: 'Kombinaciya', aliases: [] })
    expect(identityProvesName(i, 'Kombinaciya')).toBe(true)
  })

  it('proves via plain romanization when there is no alias at all', () => {
    const i = identity({ name: 'Баста', sortName: undefined, aliases: [] })
    expect(identityProvesName(i, 'Basta')).toBe(true)
  })

  it('proves a Latin-script alias of a non-Cyrillic artist', () => {
    const i = identity({ name: 'ピンク・フロイド', sortName: undefined, aliases: ['Pink Floyd'] })
    expect(identityProvesName(i, 'Pink Floyd')).toBe(true)
  })

  it('proves a surname-first sortname (MusicBrainz records them "Last, First")', () => {
    const i = identity({ name: 'Александр Серов', sortName: 'Serov, Aleksander', aliases: [] })
    expect(identityProvesName(i, 'Aleksander Serov')).toBe(true)
  })

  it('proves a surname-first sortname across scripts too', () => {
    const i = identity({ name: 'Олег Анофриев', sortName: 'Anofriev, Oleg', aliases: ['Олег Анофриев'] })
    expect(identityProvesName(i, 'Oleg Anofriyev')).toBe(true)
  })

  it('does not let word reordering match a different name', () => {
    const i = identity({ name: 'Александр Серов', sortName: 'Serov, Aleksander', aliases: [] })
    expect(identityProvesName(i, 'Aleksander Petrov')).toBe(false)
  })

  it('rejects a different artist', () => {
    expect(identityProvesName(identity(), 'Mirage')).toBe(false)
    expect(identityProvesName(identity(), '')).toBe(false)
    expect(identityProvesName(identity(), undefined)).toBe(false)
  })

  it('ignores junk aliases without crashing', () => {
    // MusicBrainz alias lists really do contain mojibake, e.g. Сплин's 'Ñïëèí'.
    const i = identity({ aliases: ['Ñïëèí', ''] })
    expect(identityProvesName(i, 'Ñïëèí')).toBe(true)
    expect(identityProvesName(i, 'Nonsense')).toBe(false)
  })
})

describe('parseArtistDoc', () => {
  it('reads name, sortname, aliases and albums', () => {
    const parsed = parseArtistDoc(docFor(disco({
      albums: [{ mbid: 'x1', title: '25-й кадр', type: 'Album', secondaryTypes: [] }],
    })))
    expect(parsed).toMatchObject({ mbid: 'a1', name: 'Сплин', sortName: 'Splean' })
    expect(parsed?.albums).toEqual([{ mbid: 'x1', title: '25-й кадр', type: 'Album', secondaryTypes: [] }])
  })

  it('drops album entries missing an id or title, and survives junk input', () => {
    const parsed = parseArtistDoc({
      id: 'a1',
      artistname: 'X',
      Albums: [{ Id: 'ok', Title: 'Good' }, { Title: 'No id' }, { Id: 'no-title' }, null],
    })
    expect(parsed?.albums.map(a => a.mbid)).toEqual(['ok'])
  })

  it('returns null without an id or name', () => {
    expect(parseArtistDoc({ artistname: 'X' })).toBeNull()
    expect(parseArtistDoc({ id: 'a1' })).toBeNull()
    expect(parseArtistDoc(null)).toBeNull()
  })
})

describe('pickDiscographyAlbum', () => {
  const parsed: ParsedItem = { raw: 'x', kind: 'album', artist: 'Splean', title: '25 Кадр' }

  it('does not accept a lone album that is not the requested title', () => {
    // The whole reason requireTitleEvidence exists: an artist having exactly one
    // release is no evidence that it's the release we asked for.
    const d = disco({ albums: [{ mbid: 'x1', title: 'Гранатовый альбом', type: 'Album' }] })
    expect(pickDiscographyAlbum(d, parsed)).toBeUndefined()
  })

  it('accepts a lone album when it does match the requested title', () => {
    const d = disco({ albums: [{ mbid: 'x1', title: '25 Кадр', type: 'Album' }] })
    expect(pickDiscographyAlbum(d, parsed)).toMatchObject({ mbid: 'x1' })
  })

  it('finds the exact title among many releases', () => {
    const d = disco({
      albums: [
        { mbid: 'x1', title: 'Реверсивная хроника событий', type: 'Album' },
        { mbid: 'x2', title: '25 Кадр', type: 'Album' },
        { mbid: 'x3', title: 'Гранатовый альбом', type: 'Album' },
      ],
    })
    expect(pickDiscographyAlbum(d, parsed)).toMatchObject({ mbid: 'x2' })
  })

  it('prefers the studio album over a same-titled compilation', () => {
    const d = disco({
      albums: [
        { mbid: 'comp', title: '25 Кадр', type: 'Album', secondaryTypes: ['Compilation'] },
        { mbid: 'studio', title: '25 Кадр', type: 'Album', secondaryTypes: [] },
      ],
    })
    expect(pickDiscographyAlbum(d, parsed)).toMatchObject({ mbid: 'studio' })
  })

  it('returns undefined for an empty discography', () => {
    expect(pickDiscographyAlbum(disco(), parsed)).toBeUndefined()
  })

  it('resolves the real Pink Floyd case the text search cannot', () => {
    const pf: ParsedItem = { raw: 'x', kind: 'album', artist: 'Pink Floyd', title: 'The Wall' }
    const d = disco({
      name: 'Pink Floyd',
      sortName: 'Pink Floyd',
      aliases: [],
      albums: [
        { mbid: 'live', title: 'The Wall', type: 'Album', secondaryTypes: ['Live'] },
        { mbid: 'wall', title: 'The Wall', type: 'Album', secondaryTypes: [] },
        { mbid: 'anim', title: 'Animals', type: 'Album', secondaryTypes: [] },
      ],
    })
    expect(pickDiscographyAlbum(d, pf)).toMatchObject({ mbid: 'wall' })
  })
})

describe('fetchArtistDiscography', () => {
  it('parses a successful response and caches it', async () => {
    const d = disco({ albums: [{ mbid: 'x1', title: '25 Кадр', type: 'Album' }] })
    stubFetch({ a1: docFor(d) })
    expect(await fetchArtistDiscography('a1')).toMatchObject({ name: 'Сплин' })
    expect(await fetchArtistDiscography('a1')).toMatchObject({ name: 'Сплин' })
    expect(vi.mocked(globalThis.fetch)).toHaveBeenCalledTimes(1)
  })

  it('returns null on a non-ok response instead of throwing', async () => {
    stubFetch({})
    expect(await fetchArtistDiscography('missing')).toBeNull()
  })

  it('returns null when the backend is unreachable', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new Error('ECONNREFUSED')
    }))
    expect(await fetchArtistDiscography('a1')).toBeNull()
  })
})

describe('resolveByCandidateAlias', () => {
  // The real "Мираж — Снова вместе" row: three artists released an album with
  // this exact title, and Spotify's "Mirage" romanizes to "mirazh", so the
  // matcher's artist gate rejects all three. Only the alias list settles it.
  const parsed: ParsedItem = { raw: 'x', kind: 'album', artist: 'Mirage', title: 'Снова вместе' }
  const candidates = [
    albumCandidate('Снова вместе', 'Мираж', 'mirazh-mbid'),
    albumCandidate('Снова вместе', '5sta Family', 'fivesta-mbid'),
  ]

  it('verifies the right artist via its MusicBrainz aliases', async () => {
    stubFetch({
      'mirazh-mbid': docFor(disco({ mbid: 'mirazh-mbid', name: 'Мираж', sortName: 'Mirage', aliases: ['Mirage'] })),
      'fivesta-mbid': docFor(disco({ mbid: 'fivesta-mbid', name: '5sta Family', sortName: '5sta Family', aliases: [] })),
    })
    const picked = await resolveByCandidateAlias(parsed, candidates)
    expect(picked).toBe(candidates[0])
  })

  it('returns undefined when two candidates both verify (real ambiguity)', async () => {
    stubFetch({
      'mirazh-mbid': docFor(disco({ mbid: 'mirazh-mbid', name: 'Мираж', sortName: 'Mirage', aliases: ['Mirage'] })),
      'fivesta-mbid': docFor(disco({ mbid: 'fivesta-mbid', name: 'Mirage', sortName: 'Mirage', aliases: [] })),
    })
    expect(await resolveByCandidateAlias(parsed, candidates)).toBeUndefined()
  })

  it('returns undefined when nothing verifies', async () => {
    stubFetch({
      'mirazh-mbid': docFor(disco({ mbid: 'mirazh-mbid', name: 'Мираж', sortName: 'Miraž', aliases: [] })),
      'fivesta-mbid': docFor(disco({ mbid: 'fivesta-mbid', name: '5sta Family', aliases: [] })),
    })
    expect(await resolveByCandidateAlias(parsed, candidates)).toBeUndefined()
  })

  it('does not spend a lookup when no candidate title matches exactly', async () => {
    stubFetch({})
    const off = [albumCandidate('Что-то другое', 'Мираж', 'mirazh-mbid')]
    expect(await resolveByCandidateAlias(parsed, off)).toBeUndefined()
    expect(globalThis.fetch).not.toHaveBeenCalled()
  })

  it('needs a parsed artist to verify against', async () => {
    stubFetch({})
    expect(await resolveByCandidateAlias({ raw: 'x', kind: 'album', title: 'Снова вместе' }, candidates)).toBeUndefined()
  })
})

describe('resolveAlbumViaArtist', () => {
  const parsed: ParsedItem = { raw: 'x', kind: 'album', artist: 'Smyslovye Gallyutsinatsii', title: '3000' }

  it('resolves a Latin title under a Cyrillic artist that album search cannot find', async () => {
    lookupArtist.mockResolvedValue([
      { foreignArtistId: 'sg', artistName: 'Смысловые галлюцинации' },
      { foreignArtistId: 'ts', artistName: 'Tamara Smyslova' },
    ])
    stubFetch({
      sg: docFor(disco({
        mbid: 'sg',
        name: 'Смысловые галлюцинации',
        sortName: 'Smyslovye Gallyutsinatsii',
        aliases: [],
        albums: [
          { mbid: 'g1', title: '3000', type: 'Album', secondaryTypes: [] },
          { mbid: 'g2', title: 'Оксиморон', type: 'Album', secondaryTypes: [] },
        ],
      })),
    })
    const hit = await resolveAlbumViaArtist(parsed)
    expect(hit?.album).toMatchObject({ mbid: 'g1', title: '3000' })
    expect(hit?.artist.name).toBe('Смысловые галлюцинации')
  })

  it('returns null when the artist cannot be proven', async () => {
    lookupArtist.mockResolvedValue([{ foreignArtistId: 'x', artistName: 'Something Else Entirely' }])
    stubFetch({ x: docFor(disco({ mbid: 'x', name: 'Something Else Entirely', sortName: undefined, aliases: [] })) })
    expect(await resolveAlbumViaArtist(parsed)).toBeNull()
  })

  it('returns null when the proven artist does not have the album', async () => {
    lookupArtist.mockResolvedValue([{ foreignArtistId: 'sg', artistName: 'Смысловые галлюцинации' }])
    stubFetch({
      sg: docFor(disco({
        mbid: 'sg',
        name: 'Смысловые галлюцинации',
        sortName: 'Smyslovye Gallyutsinatsii',
        aliases: [],
        albums: [{ mbid: 'g2', title: 'Оксиморон', type: 'Album' }],
      })),
    })
    expect(await resolveAlbumViaArtist(parsed)).toBeNull()
  })

  it('returns null when the artist lookup finds nothing', async () => {
    lookupArtist.mockResolvedValue([])
    stubFetch({})
    expect(await resolveAlbumViaArtist(parsed)).toBeNull()
  })

  it('survives a failing artist lookup', async () => {
    lookupArtist.mockRejectedValue(new Error('Lidarr 500'))
    stubFetch({})
    expect(await resolveAlbumViaArtist(parsed)).toBeNull()
  })

  it('caps discography fetches at 3 for distinctly-named candidates', async () => {
    lookupArtist.mockResolvedValue(
      Array.from({ length: 10 }, (_, i) => ({ foreignArtistId: `m${i}`, artistName: `Smyslovye Gallyutsinatsii ${i}` })),
    )
    stubFetch({})
    await resolveAlbumViaArtist(parsed)
    expect(vi.mocked(globalThis.fetch).mock.calls.length).toBeLessThanOrEqual(3)
  })

  it('widens to 6 for homonyms, where only the discography can disambiguate', async () => {
    // MusicBrainz holds several distinct bands called "Tribulation" / "Trial" /
    // "Century" and does not rank the one you meant first. They score identically
    // on name, so the only way to tell them apart is which released the album.
    lookupArtist.mockResolvedValue(
      Array.from({ length: 10 }, (_, i) => ({ foreignArtistId: `m${i}`, artistName: 'Tribulation' })),
    )
    stubFetch({})
    await resolveAlbumViaArtist({ raw: 'x', kind: 'album', artist: 'Tribulation', title: 'Funeral Pyre' })
    const calls = vi.mocked(globalThis.fetch).mock.calls.length
    expect(calls).toBeGreaterThan(3)
    expect(calls).toBeLessThanOrEqual(6)
  })

  it('finds the right homonym by which one actually has the album', async () => {
    lookupArtist.mockResolvedValue([
      { foreignArtistId: 'wrong1', artistName: 'Tribulation' },
      { foreignArtistId: 'wrong2', artistName: 'Tribulation' },
      { foreignArtistId: 'wrong3', artistName: 'Tribulation' },
      { foreignArtistId: 'right', artistName: 'Tribulation' },
    ])
    const shell = (mbid: string, albums: { mbid: string, title: string, type: string }[]) =>
      docFor(disco({ mbid, name: 'Tribulation', sortName: 'Tribulation', aliases: [], albums }))
    stubFetch({
      wrong1: shell('wrong1', [{ mbid: 'a', title: 'Spicy', type: 'Album' }]),
      wrong2: shell('wrong2', [{ mbid: 'b', title: 'Clown of Thorns', type: 'Album' }]),
      wrong3: shell('wrong3', []),
      right: shell('right', [{ mbid: 'fp', title: 'The Horror', type: 'Album' }, { mbid: 'x', title: 'Down Below', type: 'Album' }]),
    })
    const hit = await resolveAlbumViaArtist({ raw: 'x', kind: 'album', artist: 'Tribulation', title: 'The Horror' })
    expect(hit?.album).toMatchObject({ mbid: 'fp', title: 'The Horror' })
  })

  it('retries the lookup with a bare artist name when Spotify added a qualifier', async () => {
    // "Trial (swe)" finds nothing; "Trial" finds the band.
    lookupArtist.mockImplementation((term: string) =>
      Promise.resolve(term === 'Trial' ? [{ foreignArtistId: 'trial', artistName: 'Trial' }] : []))
    stubFetch({
      trial: docFor(disco({
        mbid: 'trial',
        name: 'Trial',
        sortName: 'Trial',
        aliases: [],
        albums: [{ mbid: 't1', title: 'Sulphery', type: 'Album', secondaryTypes: [] }],
      })),
    })
    const hit = await resolveAlbumViaArtist({ raw: 'x', kind: 'album', artist: 'Trial (swe)', title: 'Sulphery' })
    expect(hit?.album).toMatchObject({ mbid: 't1' })
    expect(lookupArtist).toHaveBeenCalledWith('Trial (swe)')
    expect(lookupArtist).toHaveBeenCalledWith('Trial')
  })

  it('reuses the artist lookup across repeated rows for the same artist', async () => {
    lookupArtist.mockResolvedValue([{ foreignArtistId: 'kino', artistName: 'Кино' }])
    stubFetch({
      kino: docFor(disco({
        mbid: 'kino',
        name: 'Кино',
        sortName: 'Kino',
        aliases: [],
        albums: [
          { mbid: 'k1', title: '45', type: 'Album', secondaryTypes: [] },
          { mbid: 'k2', title: 'Легенда', type: 'Album', secondaryTypes: [] },
        ],
      })),
    })
    const a = await resolveAlbumViaArtist({ raw: 'x', kind: 'album', artist: 'Kino', title: '45' })
    const b = await resolveAlbumViaArtist({ raw: 'x', kind: 'album', artist: 'Kino', title: 'Легенда' })
    expect(a?.album.mbid).toBe('k1')
    expect(b?.album.mbid).toBe('k2')
    // One artist lookup, one discography fetch — the second row hits both caches.
    expect(lookupArtist).toHaveBeenCalledTimes(1)
    expect(vi.mocked(globalThis.fetch)).toHaveBeenCalledTimes(1)
  })

  it('uses an identity learned from another row instead of the useless lookup', async () => {
    // The real "Kino — 45" row. Lidarr's artist lookup for "Kino" returns ten
    // Latin homographs and never surfaces Кино, so this row can only resolve
    // from an identity a sibling row ("Kino — Звезда по имени Солнце") proved.
    lookupArtist.mockResolvedValue([
      { foreignArtistId: 'latin1', artistName: 'Kino' },
      { foreignArtistId: 'latin2', artistName: 'Kino' },
    ])
    stubFetch({
      kino: docFor(disco({
        mbid: 'kino',
        name: 'Кино',
        sortName: 'Kino',
        aliases: ['Gruppa Kino'],
        albums: [
          { mbid: 'k45', title: '45', type: 'Album', secondaryTypes: [] },
          { mbid: 'k46', title: '46', type: 'Album', secondaryTypes: ['Demo'] },
        ],
      })),
      latin1: docFor(disco({ mbid: 'latin1', name: 'Kino', sortName: 'Kino', aliases: [], albums: [] })),
      latin2: docFor(disco({ mbid: 'latin2', name: 'Kino', sortName: 'Kino', aliases: [], albums: [] })),
    })

    const row = { raw: 'x', kind: 'album' as const, artist: 'Kino', title: '45' }
    // Without the learned identity the Latin homographs win and nothing resolves.
    expect(await resolveAlbumViaArtist(row)).toBeNull()

    learnArtistIdentity('Kino', 'kino', 'Кино')
    const hit = await resolveAlbumViaArtist(row)
    expect(hit?.album).toMatchObject({ mbid: 'k45', title: '45' })
    expect(hit?.artist.name).toBe('Кино')
  })

  it('falls back to the normal lookup when the learned identity does not have the album', async () => {
    lookupArtist.mockResolvedValue([{ foreignArtistId: 'sg', artistName: 'Смысловые галлюцинации' }])
    stubFetch({
      wrong: docFor(disco({ mbid: 'wrong', name: 'Смысловые галлюцинации', sortName: 'Smyslovye Gallyutsinatsii', aliases: [], albums: [] })),
      sg: docFor(disco({
        mbid: 'sg',
        name: 'Смысловые галлюцинации',
        sortName: 'Smyslovye Gallyutsinatsii',
        aliases: [],
        albums: [{ mbid: 'g1', title: '3000', type: 'Album', secondaryTypes: [] }],
      })),
    })
    learnArtistIdentity('Smyslovye Gallyutsinatsii', 'wrong', 'Смысловые галлюцинации')
    const hit = await resolveAlbumViaArtist(parsed)
    expect(hit?.album).toMatchObject({ mbid: 'g1' })
  })

  it('ignores incomplete learn calls and is cleared with the caches', () => {
    learnArtistIdentity(undefined, 'm', 'n')
    learnArtistIdentity('X', undefined, 'n')
    learnArtistIdentity('X', 'm', undefined)
    expect(learnedArtistIdentity('X')).toBeUndefined()

    learnArtistIdentity('Kino', 'kino', 'Кино')
    // Keyed on the normalised input name, so casing/spacing drift still hits.
    expect(learnedArtistIdentity('  kino ')).toMatchObject({ mbid: 'kino' })
    clearArtistResolveCaches()
    expect(learnedArtistIdentity('Kino')).toBeUndefined()
  })

  it('needs both an artist and a title', async () => {
    expect(await resolveAlbumViaArtist({ raw: 'x', kind: 'album', title: '3000' })).toBeNull()
    expect(await resolveAlbumViaArtist({ raw: 'x', kind: 'album', artist: 'X' })).toBeNull()
  })
})
