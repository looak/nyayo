# Match schema, version 1

The second contract nyayo owns, next to [results-schema.md](results-schema.md). A **match** is games between engines,
reported by whatever played them: a gauntlet runner ([uwanja](https://github.com/looak/uwanja)) today, OpenBench's
SPRTs later. It has its own endpoint and document because it has a different identity (no tree or branch), life cycle
(it grows while it runs and can be restarted) and comparison (inside the document) from a run. Results schema v1 is
untouched. The schema changes only by bumping `schema_version`, agreed with the owner.

The producer re-sends the **whole document** as the match grows and says the state every time; nyayo keeps no state of
its own on a producer's behalf, so a lost report costs nothing.

### OpenBench compatibility

nyayo also accepts OpenBench's top-level snapshot shape (no nested `match` object) and maps it to this schema at
ingest time. That compatibility path is for `kind: "sprt"` and `kind: "games"` reports and keeps OpenBench-specific
details under `match.settings.openbench` and `sprt.openbench`.

## Endpoints

| | |
|:--|:--|
| `PUT /api/matches/{match.id}` | the match document, JSON, optionally `Content-Encoding: gzip` |
| `PUT /api/matches/{match.id}/games/{game}` | one game's PGN so far, `application/x-chess-pgn`, optionally gzipped |
| `GET /api/matches/{match.id}/games/{game}` | the stored PGN, open like the pages |

`Authorization: Bearer <token>`, the per-machine tokens made in the admin (the same as for runs). Responses are plain
text: `201` created, `200` updated (or an identical resend of a sealed game), `401` bad token, `400` invalid with the
reason, `404` an unknown match (PUT the match first), `409` a change that isn't allowed, naming the field.

Ids are `[A-Za-z0-9._-]`, starting with a letter or digit, at most 128 characters for a match and 64 for a game; they
end up in URLs and file names.

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
  "machines": []                                     // OpenBench: [{ "name": ..., "games": n }]
}
```

Counts are always from the subject's side. `results.wins + draws + losses` must equal `games`. Unknown fields are kept
in the archive and ignored by the index.

`settings.runner` and `settings.pgn_options` say what produced the games and what its PGN comments contain. nyayo reads
whatever the comments carry, so a match played with fewer options just shows less; nothing needs playing again.
Statistics (Elo, its error, the performance rating) are computed by nyayo from the counts and never taken from the
producer; `sprt.llr` and `sprt.result` are the producer's own and shown as such.

### What may change under one id

`state`, `updated_at`, `finished_at`, the counts in `results`, `sprt.llr` and `sprt.result`, `machines`, `pgn`,
`progress`, and each game's `state`, `result`, `termination` and `fault`. **Counts only grow**, across restarts too (a
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

## Storage

Each accepted match document is archived unchanged as `archive/matches/<id>.json`, replaced on every update, with a
`<id>.meta.json` naming the machine that sent it. Each game's PGN is `games/<match id>/<game>.pgn`, replaced on every
PUT. The database is an index: matches, per-opponent results, snapshots (the counts each time they change, the basis
of the progress graph) and a row per game (result, termination, colour, opponent). `manage.py purge_games --match ID |
--older-than DAYS` removes games' files and rows without touching a match's document, counts or snapshots.
