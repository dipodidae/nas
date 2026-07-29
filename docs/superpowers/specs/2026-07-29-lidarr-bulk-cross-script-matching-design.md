# lidarr-bulk: cross-script and release-form matching

Date: 2026-07-29
Status: implemented, deployed

## Problem

Queueing a Russian-language Spotify playlist ("Слово пацана" soundtracks) produced
26 "needs your pick" rows out of 39, with candidate lists that were pure noise —
`Basta — На заре` offered ten Swedish `Bästa` compilations; `AIGEL — Пыяла`
offered ten `Angel …` records. Two further complaints surfaced during
investigation: valid titles were being split into two separate rows, and even
canonical Latin albums often returned ten irrelevant options with a combined
release at the top.

Three independent root causes, all confirmed against the live Lidarr:

### 1. Mixed-script queries are silently half-dropped

Spotify supplies a *romanized artist* with a *native-script title*. The code
joined them into one lookup term, and Lidarr's search (MusicBrainz behind it)
discards the non-Latin half:

| Term | Result |
| --- | --- |
| `Basta На заре` (what the app sent) | 10 hits, all Swedish `Bästa`; Cyrillic ignored |
| `На заре` | ✅ real `Баста // На заре` present |
| `Баста На заре` | ✅ real album at position 0 |
| `AIGEL Пыяла` | 10× `Angel …`; title contributed nothing |
| `Пыяла` | ✅ `Аигел // Пыяла` at position 0 |

### 2. The comparison key was blind to Cyrillic

`normKey` NFKD-folds `ö → o` but does nothing across writing systems, so `Баста`
vs `Basta` scored 0. Even when the correct row *was* retrieved, ranking buried it
and auto-match could not fire.

### 3. Retrieval frequently omits the album entirely

Not a ranking problem — the release is absent from the page:

| Term | Real album's position |
| --- | --- |
| `Pink Floyd The Wall` | **not in the top 10**; tribute/cover albums outrank it |
| `Metallica Master of Puppets` | #4, behind two joke covers |
| `Iron Maiden Powerslave` | #0, but #1 is the combined `Powerslave / Single Collection 2` |

Separately, `parsers.ts` split album blobs on `[\n,;\t]` whenever the text had no
double-quote, so any comma inside a title became a row break:
`Городок, что я выдумал`, `I'm Wide Awake, It's Morning`, and
`Emerson, Lake & Palmer` each became two bogus rows.

## Key upstream facts this design leans on

- `api.lidarr.audio/api/v0.4/artist/{mbid}` returns `artistaliases`, `sortname`
  and the **complete** `Albums[]` (`Id`/`Title`/`Type`/`SecondaryTypes`) in one
  call (~0.1 s; Pink Floyd's is 649 release groups / 160 KB). It requires a
  `User-Agent` header — it 403s without one.
- Those fields carry exactly Spotify's spelling: `Сплин` → `sortname: "Splean"`,
  `Комбинация` → `aliases: ["Kombinaciya"]`, `Аигел` → `["Aigel"]`. Aliases are
  script-agnostic (Pink Floyd's include `핑크 플로이드`, `ピンク・フロイド`).
- A plain BGN/PCGN romanization table matches 18/20 real artist pairs; the two
  misses (`Мираж`→`Mirage`, `Сплин`→`Splean`) are aliases, not transliterations.
- MusicBrainz sortnames are recorded surname-first (`Serov, Aleksander`).
- Lidarr's artist lookup does **not** surface a Cyrillic artist behind a short
  Latin homograph: `Basta`, `Kino`, `Forum`, `7B` return ten Latin artists and no
  Cyrillic one. Multi-word names (`Rok-Ostrova`, `Igor Talkov`) resolve at 0–1.

## Design

Dependency layering is `text → script → matching → artist-resolve → jobs`.

### `server/utils/text.ts` (new)

`normKey`, `levenshtein`, `similarity` — extracted so the romanization layer and
the matcher share them without a cycle. `normKey` is **not** re-exported from
`matching.ts`: Nuxt auto-imports every `server/utils` module, and a re-export
makes one symbol resolvable from two paths.

### `server/utils/script.ts` (new)

- `dominantScript()` — `latin | cyrillic | other | none`; digits and punctuation
  do not vote, so `2000$` is `none` ("no evidence"), never a mismatch.
- `isMixedScript(artist, title)` — requires evidence on both sides.
- `romanizeVariants()` — Cyrillic→Latin (Russian + Ukrainian/Belarusian/Serbian
  extras), emitting *multiple* readings for genuinely ambiguous letters
  (`ц→ts|c`, `ж→zh|j`, `я→ya|ia`, `й→y|i`, `щ→shch|sch`, `х→kh|h`, `ю→yu|iu`,
  `ё→yo|e`, `е→e|ye`), capped at 8 variants. The multi-variant part is what
  rescues `Комбинация→Kombinaciya`.
- `bestCrossScriptSimilarity()` — best score over the variant cross-product;
  degrades to plain `normKey` similarity for same-script input.

**NFC before, NFKD after.** Romanization must run on composed text: NFKD
decomposes `й` into `и` + combining breve, and the subsequent mark strip leaves a
bare `и`, silently turning `Май` into `mai` instead of `may`.

### `server/utils/matching.ts` (extended)

`pickAutoMatch` becomes two-stage instead of one hard AND across fields:

1. **Artist gate** — a candidate is considered only if its artist is the artist
   asked for: cross-script exact, `≥ 0.9` for transliteration drift, or
   `artistProven` from the alias path. This is what rejects "Powerslave by Some
   Cover Band" on title alone.
2. **Title decision** among gated candidates — an exact key hit wins outright;
   otherwise a fuzzy winner must clear `0.95` *and* beat the runner-up by `0.05`.

A proven artist lowers the artist bar, **never** the title bar: `25 Кадр` against
Сплин's real `25-й кадр` stays a user decision.

Also added:

- **Release-form penalties** (small, subtractive — they order equal candidates
  and never promote one past the threshold): non-`Album` types, `SecondaryTypes`
  of Compilation/Live/Demo/Remix/Mixtape/Interview/Soundtrack, and a spaced ` / `
  in the title (the combined-pressing smell). Waived when the user's own title
  asks for that form. An unspaced slash (`AC/DC`) is not penalised.
- **Subtitle tolerance**, `normKeySubtitle`, scored at a 0.97 ceiling — and
  **off by default**. This default is load-bearing: asked for `The Wall`, the
  search page came back without the real album but *with*
  `The Wall: The Film Soundtrack`, which subtitle-strips to an apparent near-exact
  hit and was auto-added during testing. Absence from a truncated ten-row page is
  not evidence that nothing better exists. Inside a complete discography it is,
  so `pickDiscographyAlbum` opts in.
- **`rankCandidates`** uses cross-script scores, weights artist 1.2× title, and
  subtracts release penalties, with a strict-key tiebreak so a literal
  `Powerslave` still displays above `Powerslave (Remaster)`. Ranking may use
  subtitle evidence freely — it only orders, it never decides.

### `server/utils/artist-resolve.ts` (new)

- `identityProvesName()` — checks primary name, sortname and every alias, folded
  cross-script *and* as a word multiset (so `Serov, Aleksander` ≡
  `Aleksander Serov`). Threshold is `0.9`, deliberately equal to the matcher's
  `ARTIST_IDENTITY` so there is one notion of artist identity in the codebase.
- `pickDiscographyAlbum()` — reuses `pickAutoMatch` with `artistProven` and
  `requireTitleEvidence`. The latter suppresses the "only one candidate, take it"
  shortcut, which is valid when Lidarr's search chose the pool but wrong when we
  did: an artist having exactly one release says nothing about whether it is the
  release requested.
- `resolveByCandidateAlias()` — we already hold rows whose *title* is an exact
  hit; only the artist's identity is open. One alias fetch per distinct artist
  (max 3), and a result only when exactly one verifies. This settles
  `Мираж` ≡ `Mirage`.
- `resolveAlbumViaArtist()` — prove the artist, then find the album in their
  discography. Returns null when the artist can't be proven or two proven artists
  both plausibly own the title.
- TTL cache (1 h, 200 entries) on both the artist lookup and the discography.
  Playlists repeat artists heavily — the triggering playlist had five Kino albums.

### `server/utils/jobs.ts` (extended)

`searchAlbumCandidates` is a cascade; every stage short-circuits as soon as the
matcher can commit, so ordinary Latin rows still cost exactly one lookup:

1. Text search over `albumQueryVariations`, which now **leads with the bare
   native-script title** for mixed-script rows, so the poison combined term is
   usually never spent.
2. Candidate-side alias verification.
3. Artist-first discography resolution → `lookupAlbum('lidarr:<mbid>')` so the
   add path is unchanged. Original hits are kept behind the resolved one so a
   wrong resolution stays correctable from the picker.

Every network stage is wrapped so a metadata-backend outage degrades to today's
behaviour rather than failing the job.

### `server/utils/parsers.ts` (fixed)

Album blobs no longer split on bare commas (`\n`, `;`, `\t` only). `parseCsvPair`
is now strict — either both fields are genuinely quoted, or the line has exactly
one comma *and* no dash/pipe/`by` separator — so
`Bright Eyes - I'm Wide Awake, It's Morning` stays one row. A line carrying two or
more dash separators additionally queries the whole raw line, since `A - B - C`
could legitimately split either way.

## Round 2

The first round left 9 rows needing a pick. Checking each against the artist's
*real* MusicBrainz discography showed 2 were fixable and 7 were genuinely absent
upstream — so the remaining work was one matcher gap, one retrieval gap, and an
honesty problem in how the unresolvable rows were presented.

### Cross-row artist learning (`learnArtistIdentity`)

`Кино`'s sortname is literally `Kino` and `7Б`'s aliases include `7B`, so identity
proof works — but `lookupArtist("Kino")` returns ten Latin homographs and never
surfaces Кино, so the artist path could never start. Meanwhile *other rows in the
same job* had already identified Кино by title.

So a resolved row now records `inputName → {mbid, name}`, `resolveAlbumViaArtist`
consults that first, and `jobs.ts` gained a **phase A2** that retries still-
unresolved album rows once identities have been learned. `Kino — 45` — a bare
numeric title no search can disambiguate — resolves from Кино's discography.

### Unique clear winner in a complete catalogue (`completeCatalogue`)

Same justification as `allowSubtitleMatch`: within a proven artist's entire
catalogue, the runner-up's score is real information. Accept the best title if it
clears 0.7 **and** beats the runner-up by 0.25. Calibrated on real data:

| | best vs runner-up | outcome |
| --- | --- | --- |
| Сплин `25 Кадр` → `25-й кадр` | 0.78 vs 0.31 | accept |
| 7Б `Я умираю, но не сдаюсь!` | 0.42 vs 0.36 | refuse — album absent |
| Никитины `Городок, что я выдумал` | 0.34 vs 0.34 | refuse — it's a song |
| Кино `Виктор Цой 55` | 0.41 vs 0.32 | refuse — album absent |

When the album isn't in the catalogue, everything scores low *and close together* —
that closeness is the signal to stay out of it.

### Honest not-found instead of a pick list full of strangers

`808 Squadliners Beatz — 2000$` offered nine unrelated `808` albums under
"Multiple matches — pick the right one". When nothing in the result set is even
plausibly by the requested artist, the honest answer is that the release wasn't
found; the user was only ever going to hit Skip.

**Whole-string similarity cannot make that call.** Measured against live results:

```
0.714  Татьяна Никитина и Сергей Никитин → "Татьяна и Сергей Никитины"   ← RIGHT
0.750  Aleksander Serov                  → "Aleksander Jež"             ← WRONG
```

The correct artist scores *below* the coincidental one, so no threshold on that
score separates them — the first attempt at this (a second, lower similarity
threshold) was simply unsound. Word coverage does separate them: Russian
declension alters one word ending (`Никитин`→`Никитины`, 0.875 as a token) and
leaves the rest intact, while a coincidental name shares one word out of two.
`hasPlausibleArtist` therefore asks what fraction of the requested name's words
have a close counterpart in the candidate's, under both raw and romanized
tokenizations, and requires 0.75.

## Results

Replayed against live Lidarr with all the real rows (32: the original 26
needs-pick entries, the not-found ones, and the Latin complaints):

**25/32 auto-matched, 1 needs-pick, 6 not-found** — from roughly 3 auto-matched
and 26 picks before.

The single remaining pick is correct behaviour:
`Татьяна Никитина и Сергей Никитин` is *kept* as a pick because the real artist is
in the list under MusicBrainz's declined spelling, so there is a genuine choice to
make. The 6 not-found rows were each verified absent from the artist's real
discography.

Notable fixes beyond the Cyrillic scope: `Pink Floyd — The Wall`,
`Metallica — Master of Puppets` and `Iron Maiden — Powerslave` auto-match the
correct studio release instead of a tribute album or combined pressing.

Gates: `pnpm lint`, `pnpm typecheck`, `pnpm vitest run` (200 tests) all pass.

## Deliberately out of scope

- Auto-matching across Russian morphological variation in artist names
  (`Никитин` vs `Никитины`); such rows are kept as a pick rather than discarded.
- Romanization tables for scripts other than Cyrillic — the alias path already
  handles Greek/CJK/Hebrew/Arabic without one.
- Rows genuinely absent from MusicBrainz (the `Garavari` entries).

## Deploy

`docker compose up -d --build lidarr-bulk`
