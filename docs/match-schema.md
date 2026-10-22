# Match schema, version 1

The second contract nyayo owns, next to [results-schema.md](results-schema.md). A **match** is games between engines,
reported by whatever played them: a gauntlet runner ([uwanja](https://github.com/looak/uwanja)) today, OpenBench's
SPRTs later. It has its own endpoint and document because it has a different identity (no tree or branch), life cycle
(it grows while it runs and can be restarted) and comparison (inside the document) from a run. Results schema v1 is
untouched. The schema changes only by bumping `schema_version`, agreed with the owner.

The producer re-sends the **whole document** as the match grows and says the state every time; nyayo keeps no state of
its own on a producer's behalf, so a lost report costs nothing.

Optional additions that leave every existing document valid (`stats`, batches) stay within version 1. OpenBench sends
version 1 too; the top-level shape it sent before is no longer accepted (`400`, no `schema_version` at the top).

## Endpoints

| | |
|:--|:--|
| `PUT /api/matches/{match.id}` | the match document, JSON, optionally `Content-Encoding: gzip` |
| `PUT /api/matches/{match.id}/games/{game}` | one game's PGN so far, `application/x-chess-pgn`, optionally gzipped |
| `GET /api/matches/{match.id}/games/{game}` | the stored PGN, open like the pages |
| `PUT /api/matches/{match.id}/pgns/{batch}` | a batch of finished games, `application/x-bzip2` (see *Batches*) |
| `GET /api/games?…` | every stored game matching a filter, one bz2 stream (see *Export*) |

`Authorization: Bearer <token>`, the per-machine tokens made in the admin (the same as for runs). Responses are plain
text: `201` created, `200` updated (or an identical resend of a sealed game or a batch), `401` bad token, `400` invalid
with the reason, `404` an unknown match (PUT the match first), `409` a change that isn't allowed, naming the field,
`413` a body over 64 MB.

Ids are `[A-Za-z0-9._-]`, starting with a letter or digit, at most 128 characters for a match and 64 for a game or a
batch; they end up in URLs and file names. The match id `batches` is reserved (it is a directory of the games).

## The match document

```jsonc
{
  "schema_version": 1,
  "match": {
    "id": "b7d855a0-3790-499a-90ab-64eab6b99ad4",   // producer-chosen and stable; a runner uses a uuid
    "name": "elephant-1.0.0-ccrl-blitz",             // optional, shown instead of the id
    "kind": "gauntlet",                              // sprt | gauntlet | selfplay | games
    "state": "running",                              // running | finished | stopped | error
    "producer": { "name": "uwanja", "version": "0.1.0", "url": null },   // url: a link back, when there is one
    "started_at": "2026-10-15T08:00:00Z",            // ISO 8601 with a timezone
    "updated_at": "2026-10-15T08:30:00Z",            // required, when the producer last had news
    "finished_at": null,
    "host": { "name": "ryzen", "cpu": "...", "logical_cores": 32 },
    "settings": {
      "tc": "120+1", "book": "8moves_v3.pgn", "book_sha256": "...", "concurrency": 14,
      "runner": { "name": "fastchess", "version": "v1.8.2-alpha" },
      "pgn_options": ["nodes", "nps", "pv"]          // what fastchess was told to write, see Move comments
    }
  },
  "subject": {                                       // the engine under test
    "name": "Elephant", "version": "1.0.0", "commit": null, "branch": null, "repo": null, "bench": null,
    "options": { "Threads": 1, "Hash": 64 }
  },
  "opponents": [
    { "id": "barbarossa", "name": "Barbarossa", "version": "0.6.0", "options": { "Threads": 1 },
      "rating": { "value": 2465, "list": "CCRL Blitz", "as_of": "2026-09-01" } }   // or null
  ],
  "results": [                                       // one row per opponent that has played, subject's side
    { "opponent": "barbarossa", "games": 10, "wins": 5, "draws": 3, "losses": 2,
      "penta": [0, 1, 2, 2, 0],                      // LL LD DD DW WW in pairs; null when not paired
      "timelosses": { "subject": 0, "opponent": 1 },
      "crashes": { "subject": 0, "opponent": 0 }, "illegal": { "subject": 0, "opponent": 0 } }
  ],
  "progress": { "total": 8, "played": 3, "playing": 2 },   // gauntlets, with games
  "games": [                                         // the whole schedule, from the first report on
    { "number": 1, "round": 1, "opponent": "barbarossa", "subject_colour": "white",
      "state": "finished", "result": "1/2-1/2", "termination": "Draw by 3-fold repetition", "fault": null },
    { "number": 2, "round": 1, "opponent": "barbarossa", "subject_colour": "black",
      "state": "playing", "result": null, "termination": null, "fault": null }
  ],
  "sprt": null,                                      // kind sprt: elo0, elo1, alpha, beta, model, llr, lower, upper, result
  "stats": null,                                     // optional, the producer's own figures: { elo, error, model }
  "machines": []                                     // the workers, see Machines
}
```

Counts are always from the subject's side. `results.wins + draws + losses` must equal `games`. Unknown fields are kept
in the archive and ignored by the index.

`settings.runner` and `settings.pgn_options` say what produced the games and what its PGN comments contain. nyayo reads
whatever the comments carry, so a match played with fewer options just shows less; nothing needs playing again.
Statistics (Elo, its error, the performance rating) are computed by nyayo from the counts; `sprt.llr` and
`sprt.result` are the producer's own and shown as such.

**`stats`**, when a match carries it, is the producer's own Elo and error (95%) for the match, from the subject's side,
and `model` says how it was computed (`penta` or `tri`). nyayo shows it as the match's Elo, labelled as the producer's,
and keeps its own calculation for matches without it and for each opponent on the page. It is how an SPRT's figures
are OpenBench's, the source of truth for an SPRT, rather than a second calculation that disagrees with it.

### Machines

`machines` lists the workers that played a test, shown as a list on an SPRT's page, busiest first. Only `name` and
`games` are needed; the rest is shown when a producer sends it and left out when it doesn't.

| Field | Meaning |
|:--|:--|
| `name` | the machine's name |
| `games` | games it has played in this match |
| `user` | whose machine it is |
| `crashes`, `timeloss` | engine crashes and time losses on it |
| `updated_at` | when it last reported |
| `cpu` | the processor's name, as the system gives it |
| `physical_cores`, `logical_cores` | cores and hardware threads |
| `ram_mb` | memory in megabytes |
| `os` | the operating system and its release, `Linux 6.8.0` |
| `concurrency` | games it plays at a time |
| `cpu_flags` | the instruction sets it has (`AVX2`, `BMI2`, `AVX512VNNI`, ...); the notable ones show beside the row, all of them under it |

### What may change under one id

`state`, `updated_at`, `finished_at`, the counts in `results`, `sprt.llr` and `sprt.result`, `stats`, `machines`,
`pgn`, `progress`, and each game's `state`, `result`, `termination` and `fault`. **Counts only grow**, across restarts too (a
row can't disappear, `penta` and the per-side counts can't shrink). Everything else (subject, opponents, settings,
SPRT bounds, a started game's number, round, opponent and colour) is fixed once received, and a change is `409` naming
the field.

- **No state is final.** A finished, stopped or broken match may go back to `running` (OpenBench restarts tests);
  `finished_at` is cleared and set again. A gauntlet runner never restarts a finished gauntlet; that is a new match.
- **The schedule goes first.** The first report lists every game as `scheduled`. A scheduled game may be corrected
  (fastchess has the last word on colours) and the list extended; once a game has started its number, round, opponent
  and colour are fixed, and a finished game keeps its result. A `playing` game may be reported `scheduled` again: a
  resumed gauntlet replays what was cut off.
- A match that isn't `running` is never shown as playing.

Matches don't expire.

## Games

`PUT /api/matches/{id}/games/{game}` carries the PGN so far, re-sent whole after every move, so a lost request costs
nothing. `{game}` is the runner's game number (the `number` in `games`; for fastchess its index in "Started game N of
M") and must stay the same across a resume. A scheduled game that hasn't started has no PGN and is never PUT empty.

- A PGN with a final `[Result]` seals the game: further different PUTs are `409`. A byte-identical resend of the final
  PGN is `200`. While the result is `*`, a PUT may replace the game with a shorter one (a resumed game starts over).
- **A sealed game is kept as data**: nyayo's worker appends it, as it came, to its match's batches with every other
  game kept (*Batches*, *Storage*), within a second or so, and once only however often it is sent. Its id stays the
  game number. Its PGN file stays until its batch is sealed, so the game page finds it either way.
- An illegal move or an unreadable PGN is `400`.
- Which engine under test and which opponent a game is comes, in order, from the schedule's entry for its number, the
  tags `[Subject "white|black"]` and `[Opponent "<opponent id>"]`, then the players' names matched against the match's
  engines; blank when ambiguous (an engine playing a copy of itself by name).
- `[TimeControl "120+1"]` (else `settings.tc`) gives the clocks. `base+increment` in seconds; anything else and the
  clocks are left out.

### Move comments

Written by fastchess with `-pgnout`, as v1.8.2-alpha does with `nodes=true nps=true pv=true` (uwanja's default;
`pgn_options` takes `nodes`, `seldepth`, `nps`, `hashfull`, `tbhits`, `pv`, and fastchess's `timeleft` and
`latency`):

```
2. Nf3 {+0.28/11 0.116s, n=278950, nps=0, pv="g1f3 d7d5 b1c3 b8c6"}
d5 {-0.28/10 0.089s, n=213535, nps=0, pv="d7d5 b1c3 d5e4"}
88. Rh2 {+M3/3 0.000s, n=227, nps=0, pv="e2h2 g8f8 h2h8"}
Kf8 {-M2/2 0.000s, n=21, nps=0, pv="g8f8 h2h8"}
89. Rh8# {+M1/1 0.000s, n=19, nps=0, pv="h2h8", White mates} 1-0
```

| Part | Meaning |
|:--|:--|
| `+0.28/11 0.116s` | the mover's score in pawns, with a sign (`0.00` has none), the depth, the time the move took |
| `+M9`, `-M8` | a mate; the distance is in **plies** (`+M9` is mate in 5), nyayo shows moves |
| `/0 0.116s` | a move with no score (no score, no depth), only the time |
| `{book}` | a book move: no time, costs nothing on the clock |
| `n=` `sd=` `nps=` `hashfull=` `tbhits=` | nodes, seldepth, nps, hash fill, tablebase hits |
| `tl=1.855s` `latency=0.000s` | time left after the move and the engine's latency; only in fastchess's own final PGN |
| `pv="g1f3 d7d5"` | the principal variation as **UCI** moves in double quotes (the moves themselves are SAN), shown as SAN |
| trailing text | the reason for the end, after the options |

A value the engine didn't report is written as **0**: nyayo treats `n=0` and `nps=0` as not reported, and so draws no
nodes or nps graph for an engine that sends none (Elephant sends no nps, seldepth, hashfull or tbhits). Black moves
carry no move number, except a black move that starts the game (`1... e5`).

**Live and final PGNs differ in two fields.** uwanja rebuilds a game in progress from fastchess's log, which doesn't
carry `tl=` or `latency=`; fastchess's final PGN has them when the options are on. The clocks use `tl=` when a move
has it and reconstruct them from the time control and each move's time otherwise, so a live game's clock is an
estimate and a finished game's is fastchess's own.

OpenBench's games are cutechess's, stripped by its worker to the 7-tag roster, `FEN`, `TimeControl`, `Variant` and
`ScaleFactor` (and `GameEndTime` for `VERBOSE`), dropping `Termination`. Every move keeps cutechess's comment in its own
layout, `{<score> <depth>/<seldepth> <ms> <nodes>}` (`{+0.35 12/18 104 1532876}`, `{book}` for a book move): the worker
means `COMPACT` to keep only the score but keeps the whole comment, so both modes carry the same per move.

## Batches

Games kept **as data**, for training: finished games sent in bulk once played, not followed live. A producer that sends
batches doesn't PUT the same games one by one.

`PUT /api/matches/{id}/pgns/{batch}` carries an **upload** of finished games, the PGNs one after the other. nyayo
takes it without reading it and its worker reads it within a second or so, in the background; nothing the producer
sends waits for that.

- **Body:** `application/x-bzip2`, a bz2-compressed PGN (concatenated bz2 streams are one upload); or
  `application/x-chess-pgn`, plain or with `Content-Encoding: gzip`, for producers that don't use bz2. At most 64 MB
  as sent (`413`), 256 MB decompressed.
- **`{batch}`** is the producer's name for the upload, stable across resends. OpenBench sends one per workload,
  `<result id>.<book index>` (the name its worker gives the upload, `<test>.<result>.<book index>.pgn.bz2`, without the
  test).
- **The answer** checks only what needs no reading: `202` taken and waiting for the worker; `200` the same bytes
  (sha256 of the body as sent) as an upload of that name already waiting or done, nothing added; `409` different
  bytes under that name; `404` a match nyayo doesn't know (PUT the match first); `400` a bad name, `Content-Type` or
  `Content-Encoding`. A bad upload is no longer a `400`: it is refused by the worker, below.
- **Every game it holds**, checked by the worker before any of it is kept: it decompresses, holds at least one game,
  every game reads (headers and moves) and every move is legal, every `[Result]` is final (`1-0`, `0-1`,
  `1/2-1/2`), and every game says who played whom: **`[Subject "white|black"]`**, the colour the engine under test
  played, and **`[Opponent "<opponent id>"]`**, one of the match's opponents. Nothing is matched by name; a game
  without both tags refuses its upload. OpenBench's worker writes them (see *OpenBench*).
- **States**, on the match page and in the admin: **pending** (taken, not read yet), **done** (every game kept), or
  **refused** with the reason, naming the first game that failed by its place in the upload and, for a move, the ply.
  A refused upload keeps none of its games. Sent again, the same bytes or fixed ones under the same name, it is
  `202` and read again; it can also be read again from the admin.
- **A game's id** is `<upload>.<n>`, its place in the upload from 1 (`9311.48.5`), as single games' ids are the
  producer's: a game page is `/matches/{id}/games/9311.48.5`.
- An upload adds no counts: the match document's `results` stay the counts, and an upload's games needn't add up to
  them (a stopped workload uploads games whose results were refused).

nyayo keeps the games in **batches of its own**, 64 games each, one match per batch, every game as it came
(*Storage*); an upload's games may span two batches, and a batch hold several uploads'. They are apart from the
per-game files and never purged with them.

### Export

`GET /api/games` streams every stored game that matches, as **one bz2 stream**: each of nyayo's batches, sealed or
still being filled, as a bz2 stream of its own, concatenated (a valid multi-stream bz2 file; Python's `bz2` and
`bzip2 -d` read it whole), with sealed single games not in a batch yet compressed and added, so it is every game
nyayo has, each once. An upload
still waiting for the worker isn't in it yet. Open like the pages. `manage.py export_games` takes the same filters and writes
a file.

| Filter | Matches |
|:--|:--|
| `kind` | `sprt`, `gauntlet`, `selfplay`, `games` |
| `engine` | the subject's `name` |
| `branch`, `commit` | the subject's `branch`, or its `commit` (a prefix of at least 7) |
| `tc`, `book` | `settings.tc`, `settings.book` |
| `since`, `until` | when the batch was begun or the game received, ISO 8601 dates |
| `match` | one match id |

Filters combine with *and*; no filter is every game. Matches are filtered on what is indexed already; there is no index
of positions.

## OpenBench

OpenBench (the fork, [looak/openbench_eg](https://github.com/looak/openbench_eg)) sends a test as version 1, and is
the source of truth for an SPRT: its
LLR, bounds and result, and with `stats` its Elo and error; nyayo computes no SPRT. One document per test, after every
batch of results and every action by hand:

| Field | From the test |
|:--|:--|
| `match.id` | `openbench-<instance>-<test id>` (`NYAYO_INSTANCE`; bobby's is `openbench`) |
| `match.kind` | `sprt` for an SPRT, `games` for a fixed number of games |
| `match.state` | finished (passed or failed), stopped (deleted, or stopped by hand), error (a bench mismatch), else running |
| `match.producer` | `{ "name": "OpenBench", "version": "<instance>", "url": "<absolute test page>" }` |
| `match.started_at`, `finished_at` | the test's creation, and its end once finished |
| `match.settings` | `tc` (dev's time control), `book`, `runner` `{ "name": "cutechess-ob" }`, `pgn_options` `[]`; OpenBench's own (`upload_pgns`, adjudication, syzygy, scaling, base's time control) under `openbench` |
| `subject` | `name`: the engine (`Elephant`, OpenBench's engine name), `branch`: dev's branch, `commit`: dev's sha, `repo`, `bench`, `options` |
| `opponents` | one, `id` **`base`**, `name`: base's engine, `branch`: base's branch, `commit`, `repo`, `bench`, `options` |
| `results` | one row, opponent `base`, with `penta`; OpenBench counts crashes and time losses without knowing the side, so they are reported under `subject` and `opponent` is 0 |
| `sprt` | `elo0`, `elo1`, `alpha`, `beta`, `model` (`penta` or `tri`), `llr`, `lower`, `upper`, `result` (`passed`, `failed` or null) |
| `stats` | OpenBench's Elo and error, as its test page shows them |
| `progress`, `games` | null: an OpenBench test has no schedule; games played is `results.games` |
| `machines` | one per worker, see *Machines*: its name, `user`, `games`, `crashes`, `timeloss`, and from the worker's own record the `cpu`, cores, `ram_mb`, `os`, `concurrency` and `cpu_flags` |

The branch is the subject's and opponent's `branch`, never their name: a test of `feature-x` against `main` is
*Elephant feature-x* against *Elephant main* on the page.

**Games** come as uploads, one per workload: the `.pgn.bz2` the worker uploaded to OpenBench, forwarded unchanged as
`PUT .../pgns/<result id>.<book index>` (*Batches*). The worker names and tags every game before it uploads: its
players are `<engine> <branch>` (`Elephant feature-x`, `Elephant main`), rewritten from cutechess's `<engine>-dev` and
`<engine>-base`, and it carries `[Subject]` (the colour dev played) and `[Opponent "base"]`. A test of a branch
against itself has the same name on both sides; the tags still tell them apart. Tests from before the worker did this
are sent again from OpenBench's tars by `report_to_nyayo --games`, which names and tags their games on the server
first. A game without the tags refuses its upload.

The games are as cutechess-ob writes them and OpenBench's worker strips them: the 7-tag roster, `FEN`, `TimeControl`,
`ScaleFactor` (and `Termination` from client 39 on), no move numbers, and each move's comment
`{<score> <depth>/<seldepth> <milliseconds> <nodes>}` (`{+1.31 14/40 159 377044}`, `{book}`), the score the mover's
in pawns and a mate's distance in plies, as fastchess writes it. The game page reads both.

## Storage

Each accepted match document is archived unchanged as `archive/matches/<id>.json`, replaced on every update, with a
`<id>.meta.json` naming the machine that sent it. Each game's PGN is `games/<match id>/<game>.pgn`, replaced on every
PUT. The database is an index: matches, per-opponent results, snapshots (the counts each time they change, the basis
of the progress graph) and a row per game (result, termination, colour, opponent, and the batch it is kept in).
`manage.py purge_games --match ID | --older-than DAYS` removes games' files and rows without touching a match's
document, counts or snapshots. A finished game is removed only once its batch is sealed, so it is never lost between
the two; one that isn't is kept back and counted. Games finished before nyayo kept batches join them through
`manage.py keep_games`, not the worker, and are kept back until then.

An upload waits as sent in `uploads/<match id>/<name>` in the data directory until the worker has put its games
away, then the file goes and its row stays (name, sha256, machine, state, games, the batches they went to). A refused
upload's file stays, to be looked at or read again.

nyayo's batches are `games/batches/<match id>/<number>.pgn.zst` (`NYAYO_BATCHES_DIR`), numbered from 1 per match: 64
games each (`NYAYO_BATCH_GAMES`; every batch records its own count), fewer in a match's last, sealed short once the
match has ended and nothing has been heard of it for five minutes (`NYAYO_SEAL_AFTER`). The batch being filled is
plain PGN, `<number>.pgn`, compressed with zstd when it is sealed and never changed after. Each batch has a row
(match, number, state, games, sizes, sha256 of the stored file, the reader that read its games, and its sources:
which uploads' games it holds, in order), and each game a **card** in the index database (`NYAYO_INDEX_DB`, its own
SQLite file on local disk): its batch, its place and offset in it, its id, the subject's colour, the opponent, the
result, `Termination` and plies, and the facts below. The cards are derived: `manage.py reindex` writes them again
from the sealed batches alone.

A card's facts come from **extractors**, each with a name and a version; the index records which version of each
filled a batch, and `reindex` visits only the batches an extractor has changed for (`--only` picks one, `--rebuild`
writes every card whole, `--dry-run` counts), one batch at a time, so it can be stopped and run again:

| Extractor | Facts |
|:--|:--|
| `reader` | how the final position ended it by the rules (`checkmate`, `stalemate`, `insufficient_material`, `fifty_moves`, `repetition`), the Polyglot hashes of the start position and of the one after 16 plies, promotions by piece, the ply each side castled, captures, en passant captures, checks, the fewest pieces, material (white's less black's) after plies 20 and 40 and at the end |
| `ending` | how the game ended: by a `rule`; else the `Termination` tag (`adjudication`, `time_forfeit`, `illegal_move`, `crash`, other); else deduced; else `unexplained` |
| `eval` | the eval from white's side in centipawns at plies 20 and 40 and the largest change between two plies, from the comments, for games where every move has one |

yes/no facts (an en passant capture, an underpromotion, a promotion to a knight, mate on the board, an ending that
was inferred) are bits of one `flags` column. **An ending is deduced** only for a game with no rule, no `Termination`
and a match that reports no time loss, crash or illegal move, against the match's adjudication settings
(`settings.openbench.win_adj` and `draw_adj`, or `reindex --win-adj/--draw-adj` for a match without): a `resignation`
when the loser's last `movecount` scores are `score` centipawns or more below zero, a `draw_adjudication` when, from
move `movenumber`, both engines were within `score` for `movecount` moves each. A deduced ending is marked inferred,
so a query can tell it from one the game carried.
`purge_games` never touches batches; `manage.py purge_batches --match ID` removes a match's batches, cards and
uploads and is only run by hand. They are data to keep: a backup has to cover `games/batches/` (on bobby, the
Synology's share), not the git backup.
