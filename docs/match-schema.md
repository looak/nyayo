# Match schema, version 1

The second contract nyayo owns, next to [results-schema.md](results-schema.md). A **match** is games between engines,
reported by whatever played them: a gauntlet runner ([uwanja](https://github.com/looak/uwanja)) today, OpenBench's
SPRTs later. It has its own endpoint and document because it has a different identity (no tree or branch), life cycle
(it grows while it runs and can be restarted) and comparison (inside the document) from a run. Results schema v1 is
untouched. The schema changes only by bumping `schema_version`, agreed with the owner.

The producer re-sends the **whole document** as the match grows and says the state every time; nyayo keeps no state of
its own on a producer's behalf, so a lost report costs nothing. A producer that nyayo has told its seq may instead send
only the schedule entries that changed (*Reports by changes*), and a live game's new moves rather than its whole PGN
(*Moves by changes*); whenever the two sides disagree the whole state is sent again, and it is always accepted.

Optional additions that leave every existing document valid (`stats`, batches, `report`, the moves request) stay
within version 1. OpenBench sends
version 1 too; the top-level shape it sent before is no longer accepted (`400`, no `schema_version` at the top).

## Endpoints

| | |
|:--|:--|
| `PUT /api/matches/{match.id}` | the match document, JSON, optionally `Content-Encoding: gzip` |
| `PUT /api/matches/{match.id}/games/{game}` | one game's PGN so far, `application/x-chess-pgn`, optionally gzipped |
| `PATCH /api/matches/{match.id}/games/{game}` | a live game's new moves, `application/json`, optionally gzipped (see *Moves by changes*) |
| `GET /api/matches/{match.id}/games/{game}` | the stored PGN, open like the pages |
| `PUT /api/matches/{match.id}/pgns/{batch}` | a batch of finished games, `application/x-bzip2` (see *Batches*) |
| `GET /api/games?…` | every stored game matching a filter, one bz2 stream (see *Export*) |

`Authorization: Bearer <token>`, the per-machine tokens made in the admin (the same as for runs). Responses are plain
text: `201` created, `200` updated (or an identical resend of a sealed game or a batch), `401` bad token, `400` invalid
with the reason, `404` an unknown match (PUT the match first), `409` a change that isn't allowed, naming the field,
`413` a body over 64 MB. The body never changes meaning; what nyayo has after a report or a game request is said in
headers (`Nyayo-Seq`, `Nyayo-Schedule-Digest`, `Nyayo-Plies`, below), on the success and on the `409` alike.

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
  "machines": [],                                    // the workers, see Machines
  "report": { "seq": 42, "base": 41 }                // optional, see Reports by changes
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
`pgn`, `progress`, `report`, and each game's `state`, `result`, `termination` and `fault`. **Counts only grow**, across restarts too (a
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

### Reports by changes

A long gauntlet's schedule is most of its document, and most of it doesn't change between two reports. A producer may
send only the entries that changed, once nyayo has said what it holds. The rule: **a producer sends what changed since
the last report nyayo acknowledged, nyayo applies it only on top of what it has, and its answer says what it has
now.** When the two disagree, the producer sends the whole document, which is always accepted.

**`report`**, optional, says where a report starts:

| Field | Meaning |
|:--|:--|
| `seq` | this report's number, counting from 1 for each run of the producer and growing by at least 1 with each report |
| `base` | the seq of the report nyayo last acknowledged (its `Nyayo-Seq`), or `null` for a whole document |

- **A report without `report`, or with `base: null`, is whole**, as every report was before `report` existed: `games`
  is the whole schedule. It is always applied on the rules above (*What may change under one id*), whatever seq nyayo
  had, and nyayo's seq becomes its `seq` (none, without `report`). OpenBench sends no schedule and no `report`, and
  changes nothing.
- **A report with a `base` is a change.** Its `games` holds only the entries that changed since `base`, whole (all
  eight fields), each replacing the entry with its `number` or adding one; an entry left out is unchanged. A change
  can't drop an entry; only a whole document can, under the rules above. Everything else in the document (the
  counts, state, `progress`, `sprt`, `stats`, `machines` and the rest) stays whole, being small.
- **nyayo applies a change only on its base.** When `base` isn't nyayo's seq for the match (another report came in
  between, one was lost, or nyayo has none), the answer is `409` with nyayo's seq in `Nyayo-Seq` (left out when it has
  none) and a plain-text reason naming both, and nothing is applied: nothing is merged on a guess. A change whose
  entries break a rule is `409` naming the field, as for a whole document. On any `409` to a change, the producer's
  next report is whole.
- **nyayo's answer says what it has.** Every answer to `PUT /api/matches/{id}`, `200`, `201` and `409`, carries
  `Nyayo-Seq: <seq>` and `Nyayo-Schedule-Digest: <digest>` when nyayo holds a seq for the match; after a whole document
  without `report`, neither. The producer computes the digest over its own schedule as it was when it sent the report;
  when they differ, its next report is whole.
- **A producer sends changes only after an answer carried `Nyayo-Seq`.** Until then, and after any answer without it,
  it sends whole documents. So a nyayo that doesn't know `report` yet is only ever sent whole documents (it ignores the
  unknown field, keeps the archive whole, and answers without the headers), and the order the two sides are deployed
  in doesn't matter.
- **Whole again at the end.** A producer sends the whole document first in a run, when the match ends (finished or
  stopped), and from `uwanja report`, so the state nyayo keeps at the end was sent whole once.

#### The schedule's digest

The digest is a check that both sides hold the same schedule, not a security measure.

- **An entry's hash** is the SHA-256 of its canonical JSON: an object of exactly the eight fields an entry has
  (`number`, `round`, `opponent`, `subject_colour`, `state`, `result`, `termination`, `fault`), a field the entry
  doesn't carry as `null`, and no others; keys sorted, at every level (`fault` is an object); no whitespace (`,` and
  `:` alone as separators); ASCII, anything else escaped as `\uXXXX` with lowercase hex, as Python's
  `json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True)` writes it. Numbers are integers.
- **The digest** is the XOR of every entry's hash, written as 64 lowercase hex digits. An empty schedule's is 64 zeros.
  Every entry carries its number, so no two entries are the same and cancel out.
- **It is kept, not recomputed**: when a report changes an entry, the old entry's hash is XORed out and the new one's
  in, so either side's cost follows the entries that changed, not the schedule's length.

Worked example, a schedule of three entries:

```
{"fault":null,"number":1,"opponent":"barbarossa","result":"1/2-1/2","round":1,"state":"finished","subject_colour":"white","termination":"Draw by 3-fold repetition"}
  c31c00e90ed62959e35278b90fcfb92de04b5f1fa78b01dfcb8e84c01a199ab0
{"fault":null,"number":2,"opponent":"barbarossa","result":null,"round":1,"state":"playing","subject_colour":"black","termination":null}
  0db81474e1538aa80df49d2626e122f6ddabffd0e707b1faebe745c60bb9adb6
{"fault":{"kind":"timelosses","side":"subject"},"number":3,"opponent":"belette","result":"0-1","round":2,"state":"finished","subject_colour":"white","termination":"White loses on time"}
  3c5ba5518743232fd8af04f6f385fb73f39614544606195a51859da121507494

digest  f2ffb1cc68c680de3609e169daab60a8ce76b49b068aa97f71ec5ca730f04392
```

Game 2 then finishes, and a change report carries only it:

```
{"fault":null,"number":2,"opponent":"barbarossa","result":"0-1","round":1,"state":"finished","subject_colour":"black","termination":"Black mates"}
  0b3a62f58887d7801725b496b73c802815eb24d20691076bb327b42703b3c425

digest  f2ffb1cc…30f04392 XOR 0db81474…0bb9adb6 (out) XOR 0b3a62f5…27b3c425 (in)
      = f47dc74d0112ddf62cd8c8d94b76c27606366f99e71c1fee292cad4638fa2a01
```

## Games

`PUT /api/matches/{id}/games/{game}` carries the PGN so far, re-sent whole after every move, so a lost request costs
nothing. `{game}` is the runner's game number (the `number` in `games`; for fastchess its index in "Started game N of
M") and must stay the same across a resume. A scheduled game that hasn't started has no PGN and is never PUT empty.

- A PGN with a final `[Result]` seals the game: further different PUTs are `409`. A byte-identical resend of the final
  PGN is `200`. While the result is `*`, a PUT may replace the game with a shorter one (a resumed game starts over).
- An illegal move or an unreadable PGN is `400`.
- Which engine under test and which opponent a game is comes, in order, from the schedule's entry for its number, the
  tags `[Subject "white|black"]` and `[Opponent "<opponent id>"]`, then the players' names matched against the match's
  engines; blank when ambiguous (an engine playing a copy of itself by name).
- `[TimeControl "120+1"]` (else `settings.tc`) gives the clocks. `base+increment` in seconds; anything else and the
  clocks are left out.
- The answer carries `Nyayo-Plies: <n>`, the number of plies nyayo now holds for the game (see *Moves by changes*).

### Moves by changes

Once nyayo holds a live game, a producer may send only its new moves:

```
PATCH /api/matches/{id}/games/{game}
Content-Type: application/json

{"from": 3, "moves": [{"san": "Nc6", "comment": "-0.21/12 0.104s, n=241005, nps=0, pv=\"b8c6 f1b5\""},
                      {"san": "Bb5", "comment": "+0.30/12 0.131s, n=301772, nps=0, pv=\"f1b5 a7a6\""}]}
```

| Field | Meaning |
|:--|:--|
| `from` | the number of plies nyayo holds (its `Nyayo-Plies`), which the new moves follow |
| `moves` | the new moves in order, each `san` (the move in SAN, as in the PGN) and `comment` |
| `comment` | fastchess's comment for the move without its braces, exactly as it is in the PGN (see *Move comments*); `null` when the move has none |

- **nyayo appends only at its own end.** When `from` isn't the number of plies nyayo holds, the answer is `409` with
  that number in `Nyayo-Plies` and a plain-text reason, and nothing is appended. The producer sends the whole PGN next
  (`PUT`), which is always accepted while the game is live; a game restarted on a resume is a whole PGN, shorter.
- **A game nyayo doesn't hold is a `409`** (`Nyayo-Plies: 0`): its first send is the whole PGN, which carries its tags.
  An unknown match is `404`, as for every game request.
- **The moves are checked as a whole**: every one must be legal SAN in the position it is played in, or the answer is
  `400` naming the first bad one, and none are appended. A comment is read as in a PGN.
- **Moves never end a game.** A game ends, and is sealed, only by the whole final PGN with its `[Result]` (`PUT`), as
  before: fastchess's own final PGN, which may carry `tl=` and `latency=` the live comments lack. A `PATCH` to a sealed
  game is `409`.
- **The answer** is `200` with `Nyayo-Plies: <n>`, the plies nyayo holds after the append. An empty `moves` appends
  nothing and answers the same.
- **A producer sends moves only after a game answer carried `Nyayo-Plies`.** Until then, and after any answer without
  it, it sends the whole PGN. So a nyayo that doesn't know the moves request is only ever sent whole PGNs, and the
  order the two sides are deployed in doesn't matter.

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

`PUT /api/matches/{id}/pgns/{batch}` carries a batch of finished games, the PGNs one after the other.

- **Body:** `application/x-bzip2`, a bz2-compressed PGN; or `application/x-chess-pgn`, plain or with
  `Content-Encoding: gzip`, for producers that don't use bz2. At most 64 MB as sent (`413`).
- **`{batch}`** is the producer's name for it, stable across resends. OpenBench sends one per workload,
  `<result id>.<book index>` (the name its worker gives the upload, `<test>.<result>.<book index>.pgn.bz2`, without the
  test).
- **Checked on the way in**, before anything is stored: it decompresses, holds at least one game, every game parses
  (python-chess, headers and moves), every move is legal, and every `[Result]` is final (`1-0`, `0-1`, `1/2-1/2`). Any
  of these failing is `400` naming the game by its position in the batch; nothing of the batch is kept.
- **Sealed on arrival:** a resend with the same content (sha256 of the bytes as stored) is `200`; different content
  under a stored batch name is `409`. `404` for a match nyayo doesn't know.
- **Who played whom**, per game, as for single games: `[Subject "white|black"]` and `[Opponent "<opponent id>"]`, then
  the players' names matched against the match's engines; blank when neither tells. An `[Opponent]` that isn't one of
  the match's opponents is `400`. OpenBench tags every game (see *OpenBench*).
- A batch adds no counts: the match document's `results` stay the counts, and a batch's games needn't add up to them
  (a stopped workload uploads games whose results were refused).

Batches are stored apart from the per-game files (see *Storage*) and never purged with them.

### Export

`GET /api/games` streams every stored game that matches, as **one bz2 stream**: the stored batches concatenated (a
valid multi-stream bz2 file; Python's `bz2` and `bzip2 -d` read it whole), with sealed single games compressed and
added, so it is every game nyayo has. Open like the pages. `manage.py export_games` takes the same filters and writes
a file.

| Filter | Matches |
|:--|:--|
| `kind` | `sprt`, `gauntlet`, `selfplay`, `games` |
| `engine` | the subject's `name` |
| `branch`, `commit` | the subject's `branch`, or its `commit` (a prefix of at least 7) |
| `tc`, `book` | `settings.tc`, `settings.book` |
| `since`, `until` | when the batch or game was received, ISO 8601 dates |
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

**Games** come as batches, one per workload. Every game carries `[Subject]` (the colour dev played) and
`[Opponent "base"]`, and its players are named `<engine> <branch>` (`Elephant feature-x`, `Elephant main`), rewritten
from cutechess's `<engine>-dev` and `<engine>-base`. A test of a branch against itself has the same name on both
sides; the tags still tell them apart.

## Storage

Each accepted match document is archived unchanged as `archive/matches/<id>.json`, replaced on every update, with a
`<id>.meta.json` naming the machine that sent it. Each game's PGN is `games/<match id>/<game>.pgn`, replaced on every
PUT. The database is an index: matches, per-opponent results, snapshots (the counts each time they change, the basis
of the progress graph) and a row per game (result, termination, colour, opponent). `manage.py purge_games --match ID |
--older-than DAYS` removes games' files and rows without touching a match's document, counts or snapshots.

Batches are `games/batches/<match id>/<batch>.pgn.bz2`, bz2 whatever the upload's encoding (a plain or gzipped upload
is recompressed; the sha256 is of the stored file), with a row each (match, batch, games, bytes, sha256, machine,
received) and no row per game: picking a training set filters on the match, which is indexed already. `purge_games`
never touches them; `manage.py purge_batches --match ID` removes a match's batches and is only run by hand. They are
data to keep: a backup has to cover `games/batches/` (on bobby, the Synology's share), not the git backup.
