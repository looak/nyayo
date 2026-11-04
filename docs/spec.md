# nyayo

Repo: [looak/nyayo](https://github.com/looak/nyayo) · Python, Django, SQLite · runs on bobby :8001

What nyayo does, as the code at this commit does it. Where it is going (planned work, open decisions) is in ramani's
`nyayo.md`, *Direction*. The contracts it owns are [results-schema.md](results-schema.md) and
[match-schema.md](match-schema.md); the install is [deploy.md](deploy.md). This file links them and doesn't copy them.

## Purpose

The history of an engine's test results, in one place. Deterministic checks arrive as **runs** from the machines that
ran them; nyayo keeps them and shows how every check and every test position moves commit by commit, so a regression
is a step in a timeline and the commit that caused it is one click away. Games between engines arrive as **matches**
(gauntlets from uwanja, SPRTs and fixed-games tests from OpenBench) and nyayo shows them, and their games, live.
Finished games are also kept as data, in batches with a card of facts for each, for tuning and training, and listed
on a page of their own.

## Scope

**Does:** receive, archive and index runs and matches; compare runs along the commit graph; show runs, checks,
positions, comparisons, matches, their opponents' results and every game, live while it is played; compute Elo, its
error and a gauntlet's performance rating from counts, and show an SPRT's own Elo as its producer's; keep finished
games as data in 64-game batches, index each with how it ended, list them and export them; back itself up; expire runs
that never became history.

**Doesn't:** play games or run engines (it's not a worker: uwanja and OpenBench play, nyayo takes what they report),
track speed (NPS stays on OpenBench), know Elephant specifically (engine specifics belong in configuration, not code).
It doesn't take CI results: CI runs are sanity checks and are thrown away.

## Current

### Runs: ingest

`PUT /api/runs/{id}`, the whole run as JSON (usually `Content-Encoding: gzip`), `Authorization: Bearer <token>`.
Tokens are per machine, made and revoked in the admin. `201` created, `200` updated (the same run again or a
promotion), `400` invalid, `401` bad token, `409` a change not allowed under this id; bodies are a short plain-text
reason the client shows. Runs are at most 64 MB.

The format is **results schema v1**, owned by nyayo in [results-schema.md](results-schema.md). It changes only by
bumping `schema_version`, agreed with the owner; nyayo refuses any other version. A run is the run's identity (id,
kind, source, state, commit or tree, branch, ancestors, version, timestamps, host, build) and its checks, each with
params, metrics, the engine's verdict, `started_at`/`finished_at` and per-position cases. Timestamps must carry a
timezone.

`ingest()` archives the raw document unchanged as `archive/<id>.json` (plus `.meta.json` naming the machine),
replaced on update, then indexes it: `Machine`, `Run`, `Check`, `Metric` (narrow: run, name, value, unit, so a new
metric needs no migration) and `Case`.

### Runs: states & expiry

`wip` → `committed` → `published`, only forward. Under one id only `state`, `commit` (null to a sha, once),
`committed_at`, `published_at` and `ancestors` may change. The client promotes runs; nyayo never matches trees itself.
`manage.py expire_runs` (daily) drops wip runs after `NYAYO_WIP_DAYS` (14) and committed runs after
`NYAYO_COMMITTED_DAYS` (30). Published runs are never expired.

### Runs: comparing

- **Comparable checks**: same id, version, limit, threads, hash, experiment flag and build (compiler, config,
  assertions, exceptions, bits). `per_file` only picks positions. A position compares when its id and its EPD file's
  content hash match. Multi threaded (`deterministic: false`) checks compare on totals only. Something missing is shown
  as missing, never as a pass.
- **Baseline** per check: the newest comparable run on the nearest commit in the run's `ancestors`, published before
  committed, never wip unless picked by hand. The second comparison is the previous run on the same branch.

### Matches: ingest

A match is games between an engine under test (the subject) and one or more opponents. Its own endpoint and document,
next to runs: **match schema v1**, owned by nyayo in [match-schema.md](match-schema.md), bumped only by agreement with
the owner.

`PUT /api/matches/{id}`, the document as it stands, the producer saying the state (`running`, `finished`, `stopped`,
`error`) every time; same tokens and responses as runs. Ids are `[A-Za-z0-9._-]`, at most 128. The document is the
subject, the opponents (with their listed ratings), a row of counts per opponent from the subject's side (wins, draws,
losses, the pentanomial, time losses, crashes and illegal moves per side), the settings, and for a gauntlet its
**schedule**: every game as scheduled, playing or finished.

OpenBench reports its tests (SPRTs and fixed-games tests) through this same API, from its fork's reporter
([looak/openbench_eg](https://github.com/looak/openbench_eg)), as match schema v1 (*OpenBench* in the schema): the
subject is the engine with its branch and commit, the one opponent has the id `base`, `sprt` holds OpenBench's LLR,
bounds and result, and `stats` its Elo and error. The shape it sent before v1 is no longer accepted (`400`). The id
`batches` is reserved. OpenBench sends no schedule and always the whole document.

- **Reports by changes.** A producer that has been told nyayo's seq may send only the schedule entries that changed
  (`report.seq`, `report.base`; schema, *Reports by changes*). nyayo applies a change only on the seq it holds, compared
  and set in one statement, and answers `409` with its own seq otherwise, applying nothing; a whole document is always
  accepted. Every answer to a match `PUT` (`200`, `201`, `409`) carries `Nyayo-Seq` and `Nyayo-Schedule-Digest` once
  nyayo holds a seq for the match, and none otherwise, so a producer that sees neither sends whole documents. The
  digest is the XOR of each entry's SHA-256 over its eight fields in canonical JSON; it is kept up to date by XORing
  the changed entries out and in, so a report costs what changed and not the schedule's length (at 10,000 entries a
  change of one game took 36 ms against 256 ms for the whole report).
- **The schedule is rows**: a `ScheduleEntry` per game (the eight fields and the entry's hash), not a JSON value on the
  match. Each changed entry is checked against the row it replaces with the same rule as a whole document (below).
- **Two first documents at once** don't fail: a new match is created in a transaction that looks again under the
  database's write lock, so the second producer waits, finds the match, and its document is an update checked against
  what the first wrote: one answer is `201`, the other `200`.
- **A match is a comparison in itself**: the opponents are in the document, so matches take no part in the ancestry
  rules for checks and have no wip/committed/published life. They name the engine's commit where one is known.
- **States**: `running`, `finished`, `stopped`, `error`, and for a test reported from its creation `waiting` (no worker
  has reached running, no games), `held` (a failure nobody can place) and `failed_prechecks` (stopped on the engine's
  failure, with no games). Pages show the state as sent; a waiting, held or failed-start match is a plain match with
  no results yet. `match.verdict` (`blame`, `summary`, `detail`, `where`, `seen_on`) and each machine's `workloads`,
  `active` and `steps` are stored as sent and validated, not yet drawn (no worker cards or summary). Matches don't
  expire, except a `failed_prechecks` match with no games, which the worker deletes with its archive once
  `finished_at` plus `NYAYO_FAILED_PRECHECKS_HOURS` (48) has passed (see *The worker*).
- **No state is final**: any state may go back to `running` (OpenBench restarts tests). `finished_at` is cleared and
  set again.
- **What may change under one id**: state, `name`, `verdict`, timestamps, the counts, the producer's LLR, result and `stats`, the machines
  that played, `report`, and each game's state, result, termination and fault. Counts only grow, across restarts too. A
  started game's number, round, opponent and colour are fixed, and a finished game keeps its result; a scheduled game
  may be corrected and the list extended; only a whole document can drop an entry, and never a started one. Anything
  else is `409` naming the field.
- **Archive**: a whole document is archived as sent, `archive/matches/<id>.json` with a `.meta.json`, replaced on
  update. A report by changes isn't a document: the archive is then written from the match as it stands (the whole
  schedule from the rows, `report` `{seq, base: null}`) when the match isn't running any more and otherwise at most
  every `NYAYO_ARCHIVE_EVERY` seconds (60), so a lost database can be rebuilt from the archive.
- **Index**: `Engine` (shared by every match that used it), `Match` (with the last `report_seq` and the schedule's
  digest), `ScheduleEntry`, `MatchResult` per opponent, `MatchSnapshot`, `Game` (the index of a single game's PGN
  file), and for games kept as data `Upload` and `Batch` (below).
- **Snapshots**: an accepted report appends one when the counts differ from the last snapshot's (time, counts per
  opponent, the producer's LLR and `stats`); a re-send with the same counts adds none. They're what the SPRT card's
  graphs are drawn from. Snapshots from before `stats` existed have none, and the Elo graph uses nyayo's own figure
  for those.
- **Statistics are nyayo's**: Elo and its 95% interval per opponent and overall (from the pentanomial when it covers
  every game, else the trinomial), and for a gauntlet whose opponents all have listed ratings a performance rating.
  A producer's own figures are not used for these. Two are shown as the producer's, because OpenBench decides the
  test with them: an SPRT's LLR and result, and its `stats` (Elo, error, model), which also stand in for nyayo's
  calculation in the match list.
- **Ratings are trusted as sent** and stored with the match.

### Matches: games

A game in play is sent whole or by changes, and a finished one is kept as data.

- **Whole**: `PUT /api/matches/{id}/games/{game}`, the PGN so far, re-sent whole, so a lost request costs nothing.
  `{game}` is the producer's game number, the `number` in the schedule, and must stay the same across a resume. A game
  with a final `[Result]` is sealed (`409`, except for a byte-identical resend); a live game may be replaced by a
  shorter one, a resume starting over. An illegal move is `400`. `GET` on the same URL returns the PGN file of a game
  still held as a single file (live, or finished and not yet purged); a game kept only in a batch is read from there
  by its page and by the export, not by this `GET` (`404`).
- **By changes**: `PATCH` on the same URL, JSON `{"from": <plies nyayo holds>, "moves": [{"san", "comment"}]}`, the
  comments fastchess's without the braces. nyayo appends only at its own end: a `from` that isn't the plies held, a
  game it doesn't hold, a sealed one, or a record that no longer describes the file is `409` with `Nyayo-Plies`, and the
  whole PGN repairs it; a move that isn't legal is `400` naming it, with none appended. `200` answers carry
  `Nyayo-Plies`, and so do `PUT` answers while the game is live; a producer sends moves only after an answer carried
  it. A `PATCH` never ends a game: the final PGN, whole, does.
- **The record of a live game**: beside its PGN, `<game>.plies` (one JSON line per ply, appended once: SAN, UCI, the
  position after it, the comment as written and read, the PV as UCI moves) and `<game>.state` (the ply, position, last
  move, the latest score and depth per side, the clocks, and where the PGN's result token starts). A `PATCH` reads the
  state, pushes only the new moves onto its position, appends to the record and writes the moves at the end of the PGN
  in place: it parses no game and writes no database row, and costs the same at ply 300 as at ply 3 (about 11 ms).
  A state that doesn't describe the PGN as it is on disk is not trusted. A whole `PUT` of a live game builds both
  again; a sealed game deletes them, after logging a warning if its final PGN's moves differ from those appended (the
  final PGN stands).
- **Files**: each game is `games/<match id>/<game>.pgn`, apart from results so `manage.py purge_games [--match ID |
  --older-than DAYS]` removes it without touching the match's counts or snapshots. The `Game` row (opponent, colour,
  result, termination, times) is written when a game appears and when its result changes, not on every move. Which
  opponent and colour a game is comes from the schedule, then the `Subject` and `Opponent` tags, then the players'
  names.
- **A game that seals is kept as data**: the worker (below) reads it and appends it, as it came, to its match's batches.
  Its file and row stay until that batch is sealed, and `purge_games` removes a finished game only then, counting the
  ones it keeps back. The export takes it from its batch once it is in one.
- **Game requests are held to part of the threads** (*Deployment*): a `PUT` or `PATCH` of a game, or a batch, that finds
  every game slot taken is `503` with `Retry-After: 1`, unread.

Move comments are fastchess's, as v1.8.2-alpha writes them with `-pgnout nodes=true nps=true pv=true`: score, depth
and time, then `n=`, `nps=`, `pv="…"` (UCI), with `tl=` and `latency=` only in fastchess's own final PGN. A zero count
is "not reported". OpenBench's games are cutechess's, stripped by its worker: `{+1.31 14/40 159 377044}` (score,
depth/seldepth, milliseconds, nodes), `{book}` for a book move; the game page reads both. The details are in the
schema, *Move comments*.

### Matches: kept games, the worker and the index

`PUT /api/matches/{id}/pgns/{batch}` takes an **upload** of finished games as data to keep, not to watch: bz2
(`application/x-bzip2`), or a plain or gzipped PGN, at most 64 MB as sent and 256 MB decompressed. It reads none of it:
it checks the match, the name, the size and the type, writes the bytes to `uploads/<match id>/<name>` and answers `202`;
the same bytes again are `200`, different bytes under that name `409`, an unknown match `404`.

- **The worker** (`manage.py worker`, `nyayo-worker.service`) looks for work every second, oldest first, one thing at a
  time (a lock beside the index database keeps it to one worker). Each pending upload is read whole through the reader
  first, and **refused whole**, none of its games kept, at the first game that doesn't read, has no final `[Result]`,
  or lacks a `[Subject]` (white or black) or an `[Opponent]` that is one of the match's: nothing is matched by name.
  The reason, naming the game and the ply, is on the upload (the admin, *Uploads*, can read a refused one again) and
  the match page counts uploads waiting and refused. A refused upload sent again is read again.
- **Failed starts**: at its start and then every `NYAYO_EXPIRY_EVERY` seconds (3600) the worker deletes the matches
  that are `failed_prechecks` with a `finished_at` older than `NYAYO_FAILED_PRECHECKS_HOURS` (48), whose results count
  no games and which have no game rows, schedule entries, uploads or batches, with their rows and their archive
  (`matches/expiry.py`). Nothing else is deleted this way. A failure of the look is logged and tried again at the next.
- **Batches** are nyayo's own: 64 games each (`NYAYO_BATCH_GAMES`), one match per batch, every game as it came. The
  batch being filled is a plain file, `<number>.pgn`; at 64 games, or short once its match has ended and nothing has
  been heard of it for `NYAYO_SEAL_AFTER` seconds (300), it is compressed with zstd into `<number>.pgn.zst`, which
  never changes. They live in `NYAYO_BATCHES_DIR` (`games/batches` in the data directory unless pointed elsewhere:
  bobby's is the Synology's share). A batch row records its games, sizes, the sha256 of the stored file, the reader
  and its **sources** (which uploads' games and which single games it holds, in order); a worker stopped half way
  truncates what wasn't committed and carries on, so nothing is appended twice. A game's id is `<upload>.<n>`.
- **The reader** is python-chess, or elephant's `phant-indexer` when `NYAYO_READER` names its binary and
  `NYAYO_READER_SHA256` its hash: the worker checks both and `--version` before it starts and refuses to start on a
  mismatch. It is about 37 times faster (20,934 OpenBench games a second against 559). Each batch and upload records
  its reader.
- **The index** (`index.sqlite3`, `NYAYO_INDEX_DB`, on local disk, never on the share) has a **card** per game: its
  batch, place and offset, the producer's id, the subject's colour, the opponent, result, `Termination`, plies, and
  facts from three versioned **extractors**: `reader` (how the final position ended it by the rules, the Polyglot
  hashes of the start position and the one after 16 plies, promotions, castling, captures, checks, the fewest pieces,
  material), `ending` and `eval` (the eval at plies 20 and 40 and the largest swing, from the comments, which the
  reader may take over), and `played` (when: the game's own `GameEndTime` or `GameStartTime`, else its `Date`, else when
  nyayo received it). Yes/no facts are bits of one `flags` column. It is derived: `manage.py reindex` writes it again
  from the sealed batches alone, only for the batches an extractor has a newer version for (`--only`, `--rebuild`,
  `--dry-run`), one batch at a time, so it can be stopped and run again.
- **How a game ended** is the rule, else the game's `Termination` tag, else deduced against the match's adjudication
  settings (`settings.openbench.win_adj` and `draw_adj`) and the engines' scores in the comments, marked **inferred**
  so a query can tell it from an ending the game carried, else unexplained. Nothing is deduced for a match that
  reports time losses, crashes or illegal moves, which a stripped game can't show.
- **Migrating** what was stored before batches: `manage.py keep_games` reads every finished single game, appends it to
  its match's batches and indexes it, then checks itself (games per match, the export before and after, the reader
  against python-chess) and changes nothing in `games/` or `archive/`; `--dry-run` reads and counts. `purge_games`
  stays a separate step.
- **Purging**: `purge_games` never removes a game that isn't in a sealed batch; batches are apart from the live games,
  a match that has them can't be deleted in the admin, and `manage.py purge_batches --match ID` removes a match's
  batches, cards and uploads, by hand only.

`GET /api/games` (open, like the pages) and `manage.py export_games -o FILE` stream every stored game matching the
filters as one bz2 stream: each of the batches, sealed or open, as a stream of its own, then the finished single games
not yet in a batch, so every game is in it once. The filters are `kind`, `engine`, `branch`, `commit` (7 characters or
more), `tc`, `book`, `since`, `until` and `match`, combined with *and*. A match's page counts its stored games and links
its export. The games page below filters by match and by opening; a position is otherwise found by reading the games.

**Position datasets** (milestone 10) are made by the worker, as jobs: requested from the Exports page (`/exports`)
or `POST /api/exports` with a machine token, one at a time, and only when there is nothing else to do, a batch a
turn, so uploads never wait behind one. The worker resolves the selection to a manifest of sealed batches and their
games (V1: every game that read and has a result, ended by rule, resignation or adjudication, inferred ones included;
the filters are `nyayo.matches.selection`, which the games page shares), then feeds each batch's chosen games, as
stored, to one `phant-indexer --draw 10 --quiet --seed S` process (with `--merge` when the dataset asks for it) open
from the first batch to the last,
so positions are deduped across the dataset, and writes its `<fen> [result]` lines to `positions-<id>.txt.zst` in
`NYAYO_DATASETS_DIR`. Progress is exact (batches read of those selected, games read, positions written), on the
page and from `GET /api/exports/{id}`. The record is kept for good: the selection, the draw's settings and seed,
nyayo's and the reader's versions with the reader's sha256, the counts, the manifest with every batch's sha256, the
positions' sha256 and a free note. Deleting (`DELETE /api/exports/{id}`, with a machine token) removes the file and keeps the record; a rebuild reads the same games
again from the batches alone and checks the new sha256 against the old. Drawing needs phant-indexer as the reader
(`NYAYO_READER`); with python-chess a dataset fails saying so. A dataset left running by a worker that stopped
starts over (`docs/match-schema.md`, Position datasets). The first ply positions are taken from is by the match's opening book, counted from
where a game starts: `NYAYO_BOOK_MIN_PLY` (`8moves_v3` 4, `Endgames` 0) and `NYAYO_MIN_PLY_DEFAULT` (8) for any other,
the map kept with each dataset and each batch's minimum in its manifest. Each game goes to the reader with its
book's minimum as a `[DrawMinPly "P"]` tag in the copy fed (the stored games are untouched), which `--draw` takes on
standard input over `--min-ply` (looak/elephant#34). The Exports page (V1) is one list: every dataset, newest
first, with its state (queued, running, done, failed), age, positions and whether its file is there or was deleted
and needs rebuilding; a download link where the file is, a Rebuild button where it's gone; the dataset in progress on
top with its progress, the page refreshing while one runs; and a New dataset button, with a merge checkbox. Each row shows its draw's settings and quiet share in a line (`10/game · merged · 80% quiet`) over a bar of
the written positions' white wins, draws and black wins in green, grey and red, each share to one decimal on it, and
links to the dataset's own page (`/exports/{id}`), built like an SPRT's or a gauntlet's: tiles (positions written,
games used, quiet positions a game, when it finished, with the download or a Rebuild), the results bar, three rings
(the written positions' results, mixed ones included; the games read: used, didn't read, no result; and the
candidate positions past the first ply: written, duplicates dropped or merged into one, quiet but not drawn, not
quiet), the quiet positions a game (mean, median, 5th percentile, minimum, each with a tooltip saying what it is),
every figure the reader's `--stats` wrote (`"stats": 1`, read through `datasets.STATS`), and the settings (the seed and
book map among them) and record. No filter form yet.

### Matches: SPRT viewer

A match with an `sprt` block gets **its own page**: an SPRT is a test of one change, not a field of opponents, so it
doesn't share the gauntlet's. Everything on it is the producer's; nyayo computes no SPRT. The top card has the games
played (the results' count, which is what the test was decided on, with the pace made of it; the games kept may be more, as a worker
uploads its whole file with the games it played after its last report, or fewer, and the page then says so under *Games*), the Elo with its error (the producer's, else nyayo's from the counts), the LLR with its bounds, the time
(started, elapsed, pace, games and time to go), the test (time control, UCI options, book, Elo bounds, alpha, beta,
model) and both engines. A block in the testers' layout (Elo, SPRT, LLR, Games, Penta, as OpenBench's and fishtest's
pages show it) can be copied with a button. The results follow with the pentanomial as pillars, time losses, crashes
and illegal moves per side, and the **workers** that played it (`machines`, busiest first, with their processor,
cores, memory and instruction sets where the producer sends them; schema, *Machines*), then the games.

- The producer's LLR as a fill from zero towards the bound it is heading for, between its lower and upper bounds,
  and the result: `passed` (green), `failed` (red), or running while there is none.
- Alpha, beta, Elo0, Elo1 and the model where the producer sends them, and, from `stats`, its Elo and error labelled as
  the producer's and with the model they were computed with.
- **Progress**, from the match's snapshots: the Elo with its 95% interval, and the LLR with its bounds, each over
  games. While the test runs, the bound the LLR is heading for and an estimate of the games and the time left to it,
  from the LLR's least-squares slope over the later half of the games so far and the pace of those games. It says "too
  few reports", "the LLR isn't moving" or "past a bound" when it can't. It's an estimate: LLR isn't linear in games.
  nyayo ports no GSPRT.

A running match with no report for `NYAYO_SILENT_AFTER` minutes (30) is marked **silent** on `/matches` and on its
page, and `/matches?state=silent` lists them. It's derived when the page is drawn and never stored; the producer still
says the state, and its next report clears the mark. The 2 minute "stale" note on a match page stays as the early hint.
An SPRT has no schedule, so its games played are the results' count.

**Steps** (match schema, *Machines*). A producer that sends `steps` gets the Workers area as **cards**, in place of the
table (a match with no steps keeps the table, unchanged). Each worker is a card: its games, share, crashes, time losses,
last report, workload, specs and flags, and its walk drawn left to right from `steps` in the order sent, a column per step
and the engines one over the other where a step has a `side`. A node is passed, active (spinning, with how long it has
been open), pending (`not reached` after a failure) or failed, a failure coloured by its `blame` (`engine` red, `worker`
amber, `unclear` purple); under the walk each failed step shows its `detail`, `evidence`, `effect` and, in a `details`,
its `log`. A card's status (playing, on its checks, worker trouble, needs a look, gone quiet after 5 minutes unheard of,
on something else) is read from its steps and `active`. Above the cards is a **summary**: per engine, how many workers
compiled and benched it and how many failed, and the workers by status; it is a tally of what was sent, and nyayo applies
no rule of its own: every verdict arrives written out. With no worker yet the area says none has picked the test up.

`match.verdict` is the headline under the title (`Stopped:`, `Held:` or `Waiting:` and its summary, its detail, where,
seen on, when it stopped), and for `failed_prechecks` when it deletes itself, `finished_at` plus
`NYAYO_FAILED_PRECHECKS_HOURS` (48); the worker deletes it then, reading the same setting and the same `finished_at`. A test in
`waiting`, `held` or `failed_prechecks` with no games shows no tiles, results or progress until it has results. With steps,
*elapsed* and *pace* count from the first worker's `running` step, not from creation, and the card says *created* and
*playing since*. Each engine in the head links to `<repo>/commit/<sha>` (the engine's `repo`, else `NYAYO_GITHUB_REPO`).
The area is redrawn by `matches/workers.js` from `/matches/{id}/workers` (JSON: `html`, `open`, `live`, `state`) every 2
seconds while a step is open and every 15 seconds while the test is waiting, held or running; a change of state reloads the
page.

`/matches` and the dashboard also show the tests with no games, apart from the running cards: waiting (with a compact walk
per worker, up to three), failed starts (red, with the reason and when each goes) and held (apart). The state filter takes
all three, and the sidebar's live count leaves them out. `manage.py seed_demo` makes one test per page of the plan's mockup
(waiting with no worker, waiting with workers in trouble, held, a failed compile, a failed bench, and a running test
with workers at every stage).

`/matches` shows a card for each match being played right now, above the list (games, Elo, how it is going and when
it will be done, and the boards of up to six games in play; a silent match is left out), and colours the list's score
and Elo; its filters, like the games list's, are in a bar that opens, open while one is set. A lone running card
spans the list's width. Every list pages 64 to a page. `manage.py seed_demo` (debug mode only) also makes two workers for the worker page, `pc-wsl` with the numbers of the design board (its 48 hours, hardware, flags, toolchains, software, 31 tests and 14,208 games) and `new-box` with two hours of history (`nyayo/lab/demo.py`), and makes seventeen simulated tests: eleven SPRTs (five of them
without games: two waiting, held, two failed starts; one running with workers at every stage) and five
simulated gauntlets (running, finished, stopped with games cut off, silent, just started), with their games, to polish
the pages on.

### Lab: the models

`nyayo/lab` (app label `lab`) is where nyayo 2.0 will create, schedule and play tests itself, taking over from
OpenBench's server (looak/ramani `plans/nyayo-2.md`). It has the models, the worker's API and the pages that create a test and stop, restart or approve it (below); no scheduling page.

- **Models** are OpenBench's (openbench_eg `da629d7`), with its names and fields, trimmed of what this lab doesn't
  use: `Test`, `Result`, `Step`, `Machine` (a worker; `runs.Machine` is a token that reports runs), `Engine` (the commit
  under test; `matches.Engine` is a build as it names itself), `EngineConfig`, `Book`, `Network`, `SPSARun`,
  `SPSAParameter`, `LogEvent`, and `ServerState` (one row, the build checksum a worker restarts on, refreshed whenever
  an `EngineConfig` is saved). `Profile` is gone: `Machine.user` and `Test.author` are Django's `auth.User`. There is
  no `PGN`: an upload of games becomes one of nyayo's batches. All are registered plainly in the admin.
- **A test and its match.** A test's match has the id `openbench-<NYAYO_INSTANCE>-<test id>` (`NYAYO_INSTANCE`, default
  `openbench`; `nyayo.lab.ids.match_id`), which must be the value OpenBench had on bobby. `Test` has no link to `Match`: the id is a function of the test's, and the
  match exists only once the first document is ingested.
- **Test ids.** The first test's id is above the highest `N` in the existing `openbench-<anything>-<N>` match ids (any instance, so a wrong `NYAYO_INSTANCE` can't restart the numbering),
  rounded up to the next thousand (1234 gives 2000, 2000 gives 3000; 1 when there are none). The data migration
  `0002_seed` works it out once and sets SQLite's sequence; ids count on, and are never reused.
- **Maths.** `nyayo/lab/stats.py` (SPRT log-likelihood ratios, Elo and its interval) and `spsa_utils.py` (SPSA
  parameters and a workload's perturbations) are OpenBench's, with its notice (README, *License*); `scipy` and `numpy`
  are dependencies.
- **Seeds** (`0002_seed`): the `Elephant` engine config with its build and presets, whose time controls are written
  with a `+` (`8.0+0.08`, `40.0+0.4`, `5.0+0.05`, `20.0+0.2`; the file Elephant came from had `8.00.08` and so on, which
  OpenBench plays with no increment), and the twelve books of the lab, by name, source and sha.

### Lab: creating a test

`/create/test` (staff only) makes an SPRT or a fixed-games test; the logic is OpenBench's `create_workload.py`,
`verify_workload.py` and `modify_workload.py` (`nyayo/lab/create_test.py`, `github.py`, `modify_test.py`, `utils.py`),
with OpenBench's notice. Tunes and datagens come later.

- **The form** is filled from the chosen `EngineConfig`: its `default` preset, then the first preset; a preset's pill
  sets time control, options, workload size and whatever else it names; the *Gain* pill takes the engine's bounds and
  *Non-regression* `[-3.00, 1.00]`. Everything else OpenBench's form asks is under *Advanced*, filled in. A test
  copies its values when created, so editing the engine later changes nothing running. Elephant's scaling default is
  `BASE`, as OpenBench's page sets it for tests. A test's time controls are stored as `TimeControl.parse` gives them
  (`8.0+0.08`).
- **Worked out for you**: as a branch is typed the page asks `/create/test/lookup` (staff only), which asks GitHub for
  the commit's sha, subject, bench and name, and how far the branch is behind the base. The same is asked again, and
  decides, when the form is posted.
- **Branch suggestions.** Both branch fields are comboboxes (a small script, no library: an arrow, a list that opens on a click, filters by substring as you type, takes the arrow keys, Enter and Escape) over the names from `/create/test/branches?repo=` (staff only; the
  repository must be an engine's source, else the list is empty with an `error`): GitHub's `/branches`, all pages up to
  ten, kept a minute per repository (`github.branches`, as anonymous callers get sixty requests an hour), `main` first and
  the rest alphabetical. It never fails: GitHub down is an empty list and the reason. While loading the list says so, and when
  empty it says why. The text stays free, so a sha or any other name still works. It is asked on load and when the engine or source changes.
- **The commit** is a branch, a tag or a full sha (`collect_github_info`). A sha is named by the branch that has it
  as its head, else the pull request's branch, else the commit's first line (`branch_of_sha`); `Test.name` is the typed
  name, else that first line, else empty (the match is then named from the branches). The bench is typed under
  *Advanced*, else read from a `Bench:` / `Nodes:` line of the commit message, and a commit with neither stops the form
  with the field marked. `NYAYO_GITHUB_TOKEN` (environment) is sent to GitHub's API when set, which lifts the rate limit
  and reaches a private engine's repository. `NYAYO_GITHUB_API`, when set, replaces `https://api.github.com/repos` in every
  address the page builds from a repository, the archives the workers fetch included; only the smoke test sets it.
- **GitHub down** (no answer, a timeout, a 5xx) is not the same as GitHub saying no: with the full 40 character sha and
  the bench typed on each side, the test is made from what was typed (the source is the zipball URL of the sha, the
  name the typed one or the sha); with a branch name it says to type those, and makes nothing.
- **The allowlist.** A repository can be tested when it is the `source` of an enabled `EngineConfig`, exactly: adding
  an engine adds its repository, and a fork is not tested. A private engine needs the token.
- **Behind base.** When both sides are in the same repository and the dev commit lacks commits the base has (GitHub's
  compare), the form is not made and says how many; *Create it anyway* posts it again with `anyway`. If GitHub can't
  say, it doesn't stop it.
- **Creating** writes two `Engine` rows (dev and base), the `Test`, and a `LogEvent`, in one transaction, then calls
  `nyayo.lab.document.report_test(test)` (step 2's) so the match exists as `waiting` before a worker has seen it, and
  goes to the match page. A test made by the superuser is approved; one made by another staff user waits for approval.
- **On a lab test's match page** (an `openbench-<instance>-<N>` match with a `Test` N), the staff see *Stop* (sets
  `finished`), *Restart* (clears it and calls `steps.restart_walks`) and *Approve* (superusers only, while not
  approved), as `POST /tests/<id>/<action>`, each logged and reported again. A match of an old OpenBench test, which has
  no `Test`, shows no link to OpenBench's server, and no buttons.
### Lab: the worker's API

OpenBench's Client (`nyayo/client/`, the worker: see *The client* below) runs against nyayo: every `client*` endpoint is at the
path and with the answers it had (`nyayo/lab/worker_api.py`, from the client hook views of OpenBench's `views.py`). They are
CSRF-exempt and open on the LAN like the rest, apart from the sign-in.

- **Registration, not sign-in** (below): no username and no password. `clientWorkerInfo` takes the worker's `token` and
  starts a session on its `lab.Machine`, answering `machine_id`, a secret and the worker's `state` and `enabled`. The other
  calls name the machine and its secret and are refused if the client's version, or the build checksum (`ServerState`, which
  changes when an engine config's build settings do), is not the server's: the worker then restarts itself.
- **Worker registration** (`nyayo/lab/registration.py`; ramani plans/nyayo-2.md, *Worker registration*). `lab.Machine` is a
  worker, one row across restarts: `name` (unique without case; a second `hunt` is `hunt (2)`), `token_hash` (the sha256 of the
  token, which is `secrets.token_urlsafe(32)`, never stored), `state` (`pending`, `approved`, `turned_away`), `enabled`,
  `first_seen`, and per session `secret` and `described`. `user` is empty but on rows from before tokens, which migration
  `lab/0005` merged, a box's sessions into its newest row (results added, steps kept as far as they do not collide), as
  `approved` and enabled, with no token. The protocol, every answer JSON:
  - `POST /clientHello/` with `name`, `client_ver`, and optionally `join` (`join_code` also) and the `token` it holds. A known
    token makes no row. Otherwise it makes a `pending` row (an `approved` one, with a valid join code), logs it, and answers
    `{ worker_id, name, state, enabled, approved, token }`: the token is given once, on the hello that made the row, and a worker
    that sent a token nyayo knows is answered the same token (what the client reads every answer for). Refused: a wrong
    `client_ver` (`Bad Client Version: Expected N`), a bad or used or expired code (`403`, `Bad join code`), a name that is turned
    away (`403`, `state: turned_away`), 20 workers already pending (`429`). A pending worker with no results and no approval is
    removed after 7 days (at hello and when Machines is read).
  - `POST /clientWorkerInfo/` with `token` and `system_info` (what the client reports, with `toolchains` once it has
    described itself, and `gpus`): a session on the worker's row, with a new secret, in any state; `described` is whether
    `toolchains` has anything in it. `supported` is judged from it (`nyayo/lab/toolchains.py`): an engine's compilers `g`
    and `clang` are `g++` or `gcc` and `clang` or `clang++` (a `>=` is kept), `make` is needed, and only those are scheduled:
    an engine that builds with `rustc`, `cargo` or `dotnet` is handed to nobody. A worker that has not described itself is
    judged by the `compilers` it scanned, as before.
  - `POST /clientGetWorkload/`, in any state: `{ "wait": "turned away" }` for a turned-away worker; `{ "describe": [tools] }`
    (`gcc g++ clang clang++ dotnet rustc cargo python3 make`) until the session has described itself, which the client does by
    sending `clientWorkerInfo` again with `toolchains`; then `{ "wait": "waiting for approval" }` while pending and
    `{ "wait": "disabled" }` while disabled; else a workload, as before. Each answer is the worker being heard from.
  - A pending or turned-away worker is refused every other call of a session with `403` and `{ error, state, enabled }`: no test,
    source or setting. A disabled approved one is let through, so a workload it has finishes. `clientGetNetwork` takes the
    `token` of an approved worker. `clientGetBuildInfo` (the engines' compilers, CPU flags, systems and build path: no test and
    no source) is the one exception, open to a call with no token: the client asks before it knows whether it is approved, and needs it
    for the CPU flags it reports. It only tells a worker what to look for, and holds exactly `path`, `compilers`, `cpuflags`,
    `systems` and `private` of each engine (a test checks the keys): no source URL, no token, no setting of the server. A
    token that is sent and is not approved is refused it. `clientVersionRef`, `clientMatchRunnerVersionRef` and the fastchess file need nothing.
  - On Machines (behind the gate, each an event of `LogEvent` with who did it and the worker's id): *Approve*, *Turn away*,
    *Enable*, *Disable* (`POST /machine-action`, `id` and `action`), and join codes (`POST /machine-join-code`, `hours` 1 to 336,
    24 by default; shown once; kept as a sha256; one use; `POST /machine-code-revoke`).
- **Fastchess from the server** (`nyayo/lab/fastchess.py`). `manage.py fetch_fastchess <tag> [--api URL]` downloads an official
  release of Disservin/fastchess (the tag with or without its `v`) into `DATA_DIR/fastchess/<tag>/` with a `manifest.json` of
  each file's sha256, system and architecture (told by its name; a file whose digest GitHub gives that does not match is refused). A `.tar` or `.zip` release file is checked as it came and then replaced by the `fastchess` executable in it, named like the archive without its extension (`.exe` when it has it), with the executable's own sha256; a file that is no archive or holds none is kept as it came.
  `clientMatchRunnerVersionRef` also answers `binary: { url, sha256, version, name }` for the `os_name` and `arch` of the call
  (Linux, Windows or Darwin; `platform.machine()`), from the newest fetched release that is at least
  `NYAYO_FASTCHESS_MIN_VERSION` and has a file for them, else no `binary`. `GET /clientFastchess/<tag>/<file>` serves a file
  the manifest lists, to anyone.
- **Settings in place of `Config/config.json`** (`nyayo/lab/config.py`): `NYAYO_CLIENT_VERSION` (by default the `CLIENT_VERSION` of
  the client nyayo hands out, 54: see *The client*), `NYAYO_FASTCHESS_MIN_VERSION` (1.8.1), `NYAYO_FASTCHESS_REPO_URL` and
  `NYAYO_FASTCHESS_REPO_REF`, each from settings or the environment. Engines and books are rows.
- **The client** (`nyayo/client/`, `nyayo/lab/clientzip.py`). The worker is OpenBench's `Client/`, copied from openbench_eg at
  `6a072fa` (looak/openbench_eg#30), with OpenBench's notice on each file; its tests are pytest's, in `nyayo/tests/client/`,
  and run from `nyayo/tests/test_lab_client.py` when pytest and `nyayo/client/requirements.txt` (`requests`, `psutil`,
  `py-cpuinfo`, and optionally `zstandard`) are installed. nyayo's own `pyproject.toml` needs none of them.
  - `GET /client.zip`, with no sign-in, is a zip of the client's files (no tests, no bytecode, no `worker.token`), the same bytes
    while the files are the same, with its sha256 in an `ETag`. `clientVersionRef` answers `{ client_version, client_url,
    client_sha256 }`: the version is the `CLIENT_VERSION` of the `worker.py` that is in the zip, so the server and what it hands
    out cannot disagree (`NYAYO_CLIENT_VERSION` overrides it; `NYAYO_CLIENT_DIR` serves another copy, which the smoke test does
    to raise the version). The old `client_repo_url` and `client_repo_ref` are gone: nothing asks GitHub for the client.
  - A worker starts from that zip (the page `/worker` has the commands, and the same box, `lab/_get_a_worker.html`, is on `/machines`):
    unpack, `pip install -r requirements.txt`, `client.py --server URL [--join CODE]`. It updates itself the way it did: a call
    answered `Bad Client Version` makes `client.py` fetch `client_url` (a path is the server's own), check `client_sha256`, and
    copy every file of the zip over its own but `client.py`, which is running, and `worker.token`, which is its own, with the time
    of the copy (Python keeps compiled files by the time and size of their source, and 54 and 55 are the same size). `client.py`
    itself is not updated by an update, as before. The worker's version stays 54: the protocol did not change, only where it fetches.
  - The worker's own behaviour (hello and `worker.token`, the describe job and *wait*, `gpus`, fastchess from the server with the
    build as a fallback, a game named and tagged with its `Termination` and the whole comment of each move, steps and the retry
    the server asks for) is what *Worker registration* above and the match schema describe from the server's side.
- **Scheduling** (`get_workload.py`, OpenBench's `workloads/get_workload.py`, names only): a worker is handed an approved,
  unfinished, undeleted test it can play (the engines it can build, its threads, syzygy, `--only`, `--focus`, `--noisy`, its
  blacklist, and the tests it left after a failure nobody could place), of the highest priority, balanced by threads against
  throughput. Handing out makes the `Result`, starts the machine's walk and reports the test. A worker that asks and is given nothing is waiting, not gone: that it was heard from (`Machine.updated`) is recorded, with one small write, and the workload it had is let go (and that test told), so the Machines page's *idle* is a worker that polls and *gone quiet* one silent for 5 minutes. OpenBench wrote nothing then.
- **Results** (`utils.update_test`, from OpenBench's `utils.py`): the counts are added to the test and the result, an SPRT's
  LLR is recomputed with `stats.py` and ends the test on a bound, a games test at `max_games`, a tune moves its parameters
  within their bounds. The answer is `{}` or `{"stop": true}`.
- **Steps and verdicts** (`steps.py`, as the fork has it): `clientReportStep` records a step of the worker's walk, and once
  both engines have been through a stage, judges the failures (whose they are: the engine's stops the test, the worker's is
  retried after `STEP_RETRY_SECONDS`, 300, one nobody can place gets one more try, and a test only that worker could play
  is held). The table is in ramani's `plans/sprt-steps.md`.
- **The match.** Every change to a test (a hand-out, a step, a result, an error) ends with `document.report_test(test)`: it
  builds the match document (`document.py`, from the fork's reporter: the same document, `producer` included, with
  `producer.url` null) and passes it to `ingest` in this process, in one write transaction, so two reports of a test are
  serialised and the later is built from what the earlier left. A failure is logged and never fails the worker's call. The
  match id is `openbench-<NYAYO_INSTANCE>-<test id>`.
- **Games.** `clientSubmitPGN` stores the workload's `.pgn.bz2`, as the worker named and tagged it, as one of nyayo's uploads
  named `<result id>.<book index>` (the match is reported first if nyayo has none yet; a workload that played nothing sends
  an empty archive, which is dropped). nyayo's worker puts the games away as for any producer. Nothing is kept as OpenBench
  kept it: the PGN tar, `api_pgns` and the watcher are not here.
- **Not here:** the pages for tunes and datagens, the networks' upload (a network is a file `networks/<sha256>` under the data directory, served by
  `clientGetNetwork`), `api_*` of OpenBench's own scripts.

The smoke test, `tools/smoke/run.py` (WSL or Linux; its docstring says how to set it up), runs nyayo as deployed (gunicorn,
4 workers of 4 threads, and `manage.py worker`) with nyayo's own client playing, downloaded from `/client.zip` as a worker does and
changed only in its timers, with no checkout of openbench_eg and no password. A toy engine, a book and a stand-in for the part of GitHub's API that the create page asks (`NYAYO_GITHUB_API`, which
replaces `https://api.github.com/repos`) are served from the harness, and every test is made through the real create page,
signed in as the owner. It checks a games test (named, a base bench typed in, the walk, games put away, the pages), an SPRT to a
result, what the page refuses, approval by the owner, stop and restart, crashes, two tests at once, an idle worker that the Machines page says is idle and, once stopped, gone quiet, a worker that updates itself from here when the version goes up and keeps its token and its row, and each failure of the
verdict table it can stage (a compile error, a wrong, slow or silent bench on dev; the machine's own failures; a killed
compiler and a wrong bench on both, which hold the test; a bench that is right and then wrong). After each, the match agrees
with its test on everything fixed and on the counts, and no report was refused. A full run takes about two to three minutes
and prints its time. `--load` measures the writes and the waits of `--workers` workers on a long test.

### Pages

Every page sits in one frame, in Pi-hole's dark AdminLTE look: a sidebar, which stays in place while the page scrolls, and a top bar. nyayo's icon, in the sidebar and as the favicon (SVG, a 32 px PNG and a 180 px apple-touch-icon), is the chess elephant from Wikimedia Commons (Chess_elt45.svg by NikNaks93), bundled under `nyayo/runs/static/img/` and used under the BSD 3-Clause License (`LICENSE-elephant.txt`). Every page's coloured tiles carry an icon, large and faint behind the figures as
Pi-hole's: Font Awesome Free 6.6.0's solid icons, the twelve used and no more, unchanged, under CC BY 4.0
(`nyayo/runs/static/img/fa/`, with `LICENSE-fontawesome.txt`; `runs/_tile_icon.html` draws one). The sidebar has a status block
(whether the worker holds its lock, the game reader, uploads waiting or refused, whether the batches' directory is
there and how much it holds) and the links, grouped as Main (Dashboard), Actions (Create test, Create tune, Create datagen, Upload net, Run gauntlet), Lab (Engines, Books, Machines), Testing (Matches with the number live: running and not silent, as the list's cards count them; Runs, Compare),
Catalogue (Games with the number kept, Exports, Uploads in the admin) and System (Admin); the page's own link is marked. The
top bar says where the page is, names the host and the time zone the pages' times are in (`NYAYO_TIME_ZONE`, UTC by default; times are stored in UTC). and who is signed in, with a link to sign in or a button to sign out. Under 900 px the sidebar is one bar with a menu button: the status block goes and the links open and close with it, so the top bar's crumb keeps its room. The status is worked out only when a page's frame is drawn, never
for the game page's polled fragment. The site has one colour scheme, dark.

| URL | Shows |
|:--|:--|
| `/` | four Pi-hole tiles: the game database (its size on disk, database, index and batches, the games it keeps and how many the index has carded, uploads waiting or refused), what is being played (the matches running and games in play, or with none, the match played last), the latest SPRT (the last to reach a verdict, else the newest: its Elo, LLR, and the crashes, time losses and illegal moves of the engine under test, red when there were any) and GitHub activity (the commits pushed in the last 7 days that have a run in nyayo, read from the published runs, on how many branches, and the latest push; linked to the repository on GitHub when `NYAYO_GITHUB_REPO` names it); the games kept by the day they were played over the last 53 weeks, as GitHub draws a year of commits (Monday first, days in the site's zone); two donuts of the kept games, how they ended and how they went for the engine under test; the running matches' cards with their boards in play; the latest matches; and the book positions most games started from, with the engine's score from each and a link to their games. Its figures over the kept games are SQL over the index's cards, about 0.4 s for 45,000. The home page; the sidebar's brand links to it |
| `/runs` | runs, newest first, filtered by branch, state and tier; each with its worst verdict, solved totals, a marker per check, duration |
| `/runs/{id}` | every check against its baseline: verdict, settings, when & how long, a score bar (pass, newly pass, newly fail, fail; minimum & baseline marked), a grid of positions; then what changed |
| `/compare` | any two runs, or a run against its baselines or its branch's previous run; incomparable checks listed as such |
| `/case/{check_id}/{case_id}` | one position on a board with expected & avoided moves, a lichess analysis link, and every attempt along a branch's history |
| `/matches` | matches, newest first, filtered by kind, state (or silent) and engine; games, score and Elo (an SPRT's is its producer's) |
| `/matches/{id}/workers` | the SPRT page's Workers area alone, as JSON, for its poll |
| `/matches/{id}` | a head with the match's state and runner, Pi-hole tiles of the figures that matter (a gauntlet: played, performance, score, time to go; an SPRT: games, Elo, LLR, time to go), the settings, an opponent per card or per row of a list (the choice is remembered in a cookie): counts, score, Elo ± error, pentanomial, rating, faults; overall with the performance rating and a table to copy; an opponent can be left out of the figures from the page; a small board for every game in play, each a link to its game and drawn in the browser by chessground (`matches/minis.js`); the SPRT card when present; the games stored as data (and the uploads waiting or refused), with a link to their export; and the games, kept ones included: playing, cut off, scheduled, finished. Refreshes while the match runs. A match that isn't running never shows a game as playing |
| `/matches/{id}/games/{game}` | a large board with a bar and a clock per engine, an eval bar from white's side, a card per engine (score, depth, time, nodes, nps, PV), the moves in a scrolling list, and graphs per metric with a line per engine (the eval graph from white's side too); stepping through the game by click or arrow keys. The board is lichess's chessground (9.1.1, GPL-3.0, vendored under `nyayo/matches/static/vendor/`), drawn in the browser from every position the page embeds, so a step shows at once and a move animates; the rest of the page then follows from the server. A game is read from its file, or from its batch once it is kept |
| `/games` | Pi-hole's query log: a filter bar that opens, and every game kept as data, newest first, 64 to a page, its row tinted by a win or a loss for the engine under test: when it was played (a time only where the game has one), its id (a link to its page), white and black (from the card's colour and the match's engines), its moves, result (and win, draw or loss for the engine under test), how it ended (deduced endings marked), the book position it started from with a link to the other games from it, and the match it came from. `?match=`, `?result=` (win, draw, loss), `?ended=` (a rule such as `checkmate` or `repetition`, or another ending such as `resignation` or `time_forfeit`), `?moves_from=` and `?moves_to=` (whole moves) and `?opening=` (16 hex digits) filter it, all together; a bad `ended`, moves or opening is `400`. It reads a page of cards from the index and fills in matches and engines for those 64 rows, so it takes about 0.2 s wherever it is in the list, and it shows a game once the worker has put it away. There are no opening names: the books start 16 plies in, and lichess's table names 0.2% of the games |
| `/sign-in`, `/sign-out` | Django's own login against the admin's users (`LOGIN_URL`), for staff users only: anyone else, or a wrong password, is refused with the form's reason. `?next=` goes back to the page that asked, and only to this site. Signing out is a `POST` from the top bar |
| `/create/test`, `/create/test/lookup`, `/create/test/branches`, `/tests/{id}/{stop,restart,approve}` | the lab's page that creates a test, the lookup it asks as a branch is typed, the branch names it suggests, and the three actions on a test: all staff only, as above |
| `/create/tune`, `/create/datagen`, `/upload-net`, `/gauntlet`, `/engines`, `/books` | the actions and the lab's other pages that change things. Today each is a **stub** saying what will be there and which step of the nyayo 2.0 plan builds it. Signed out, they redirect to the sign-in page; signed in without staff rights, `403` |
| `/machines`, `/machines/<name>` | the boxes that play or report, open to read (nyayo/lab/machines.py). **A row is a worker:** `lab.Machine` is one row across restarts, by its token (*Worker registration*), so there is nothing to group; its games and tests are those of its results. A `runs.Machine` token of the same name is joined to it. The row has the name (linked to its page), the tags, the system, the threads (and sockets), what it is doing, MNPS dev / base, when it was last heard, its games and tests, and the engines it supports. **Now** is *playing* the test it is handed (linked to its match) while that has not ended and the machine was heard from in the last 5 minutes, the meaning of `active` in a match's document; *idle* when heard from and handed nothing; *gone quiet* otherwise; and by the owner's say *waiting for approval* (pending), *disabled* and *turned away*, which the status icon words too and which are never *online*. A signed-in owner sees *Approve*, *Turn away*, *Enable* and *Disable* on a worker, on the list and on its page, and a card to make join codes. A worker that supports no engine says so, with what is missing as `clientWorkerInfo` judges it (`Missing ['g', 'clang']`, `Missing make`, only C and C++ are scheduled, missing CPU flags, a system the engine is not built for, no token). The tags join to `runs.Machine` by name, ignoring case: *worker* is a `lab.Machine`, *token* (or *token revoked*) a `runs.Machine`, and a token with no worker is a row of its own with when it last reported a run or a match. The page of an **approved** worker, laid out as the design board `WorkerB.dc.html` has it, top to bottom: **the last 48 hours** (below); four coloured blocks in a row (two by two under 1100 px, one under 560);  its trouble now (a failed step the worker is blamed for, or unclear, on a test that has not ended, with its tries and `retry_at`); boxes for Hardware (with the CPU flags), Toolchains, Software and the engines it plays or why not; where it is (for someone signed in); and **every test it has played**, newest first, 64 to a page (`?page=`), each linked to its match page with games, workloads, crashes, time losses and failed steps. The tiles are *Connection* (the status, how long it was heard from in the hours on record and the share of them, the response time, when it was last heard, and what is wrong when it needs a look), *Games, all time* (with its tests, games an hour while it was up over the last day, crashes and time losses), *Last workload* (the test it last worked on, its kind, what it is against, its workloads and games on it, and how many tests of each kind it has contributed to, the kinds being those of `Test.test_mode`: SPRT, SPSA, games, datagen) and *Approval* (approved, when it was first heard from, and for someone signed in the **Enabled** switch, which posts to `/machine-action` as *disable* or *enable* and takes effect at once, and *Turn away*). **A worker waiting for approval, or turned away, has a page of its own:** a headline saying it gets no tests, what it reported (system, processor, memory, client version; nyayo does not keep the address it said hello from), *Approve* and *Turn away* for someone signed in (*Approve* only, for one already turned away), the steps of its first job (hello, hardware, toolchains, fastchess) done or not yet, and a card per language (C++, C, Rust, C#, Python) saying whether it found a tool for it and which, then Hardware, Toolchains and Software as for an approved one; it has no graph or tiles. **The 48-hour graph** is the first thing on an approved worker's page: a pillar of games for each hour, the response time as a line over them, and a strip of whether the worker was heard from (online for half an hour or more, part of the hour, offline). An hour before the first one on record is blank, not offline: no bar, a very dim cell in the strip with no outline, and only the faint line of its hour in the plot; a note in the plot's top left says how many hours of history there are (and says nothing when all 48 are on record). An hour with no games is a quiet one. The response time line, its axis and its legend entry are left out when no hour has a measurement (a development server cannot measure it). Pointing at a real hour, tapping it, or the arrow keys on the focused graph light it a little and write its details in a chip over the plot (nothing is lit, and nothing said, for a blank hour or while nothing is pointed at); it scrolls inside its box on a phone. It is drawn from **`lab.MachineHour`**, a row per worker (its lowercase name, or `machine <id>`; registration keeps one row per worker, so a restart goes on in the same row) and hour with the games reported, the seconds heard from (the time since the worker was last heard, counted when under 5 minutes, put in the hour of the call), and the sum and count of the response times measured. `lab/hours.py` adds each call to its row with one upsert statement, in `clientGetWorkload` (also when the worker is told to wait or describe itself), `clientHeartbeat`, `clientSubmitNps` and `clientSubmitResults` (the games), so a poll makes two small writes, the worker's and its hour's; a failure there never fails the worker's request. Rows are kept 30 days and `manage.py worker` prunes the older ones at its hourly look. The page reads its 48 hours in one query. **Nothing credential-like is shown:** only the fields listed in `nyayo/lab/machines.py` are read out of `info`, never `secret`, the tokens' values (only which engines it has one for), the MAC address or a token's hash; a test checks it. A worker's card on a match page links to it when a box of its name exists. **The look.** Each worker has a **status**: a network icon with its words and a tooltip that says why, never colour alone. Grey, *Offline*, when not heard from for 5 minutes; yellow, *Needs a look*, when it is online but a failed step the worker is blamed for (on a test that has not ended), or one nobody can place on a test now held, is on it, or it supports no engine; green, *Online*, otherwise, idle or playing. The tooltip opens on hover, on focus and on a tap. **Response** is measured on the server, not reported by the worker: the kernel's round-trip estimate of the TCP connection a `clientGetWorkload` or `clientHeartbeat` came in on (`tcpi_rtt` from `TCP_INFO`, microseconds, read from gunicorn's `gunicorn.socket` in the request's environ, `nyayo/lab/rtt.py`), kept in ms as `lab.Machine.rtt_ms` in the write the call makes already. It is the network's round trip, not nyayo's own processing time, and behind a reverse proxy it would be the proxy's. It can never fail a request: under `runserver`, on Windows, on a closed socket or a kernel that says 0 it is left as it was, and what a worker sends as `rtt_ms` is ignored. The page shows a dash while there is none, and none for a box that is offline. **Where** is a text of up to 100 characters per name (`lab.Place`, the name lowercase, so it outlives restarts), typed by someone signed in on the box's page and shown on the list and the page; nothing is looked up, since the workers are on a LAN. A worker with no name cannot have one. **Hardware** is the CPU, its cores, threads, sockets and how many it plays with, the instruction set, the memory (`ram_total_mb`, `huge_pages`) and the GPUs (the names in `gpus`, a list of strings or of objects with a `name`, else *none reported*), with the **CPU flags** as pills inside it, each known one with a tooltip of what it means (`nyayo/lab/cpuflags.py` is the one table; names match without case, dots or underscores; an unknown flag is a plain pill) that opens on hover, focus and tap. **Toolchains** are a row per tool the describe job asked about (`info.toolchains`, a version or null for each) with a coloured pill for its language (C++, C, Python, Rust, C#, Build) and its version, a tool that was not found greyed and *not found*; a worker that has not described itself shows the compilers it reported with each engine and its Python, and says so. **Software** is the system, Python, the client's version (against the one the server wants), fastchess, syzygy and the command line. A small icon of the system, Linux, Windows or macOS, with a *WSL* tag when `os_ver` says `microsoft-standard-WSL`. A phone sees each row as a stack. A worker with no name (a row from before the tokens) is called `machine N` by its id and cannot have a place |
| `/admin/` | machines & tokens, browsing and purging runs and matches; uploads (with the reason a refused one was refused, and a way to read it again) and batches, read-only |
| `/healthz` | liveness |

The game page is **live**: it asks every 100 ms (`NYAYO_GAME_POLL_MS`) whether the game changed, sending the version it
shows, and the server answers an empty `204` from a file stat and one row query, so a poll with nothing new is nearly
free. The clocks are reconstructed from the time control and each move's time (exact where a move says `tl=`) and run
down locally between posts; polling pauses in a hidden tab. A change still redraws the page's whole game body; the
per-ply record a `PATCH` writes is not read by the pages yet.

### Who may act

Reading is open on the LAN and stays so; anything that starts, changes or stops work needs a signed-in staff user (the
admin's users, made with `createsuperuser`). `nyayo/actions/access.py` is the one gate: `action_required` wraps a
function view and `ActionRequiredMixin` a class view, sending a signed-out visitor to `/sign-in?next=` and answering a
signed-in user without staff rights `403`. The API routes with machine tokens don't pass through it. A page that acts
is added to `nyayo/actions/urls.py` (or its own app's urls) wrapped in the gate; a stub there is replaced by changing
its line, and the stubs' table in `nyayo/actions/views.py` loses its entry. There is no lock-out after failed logins.

### Deployment

bobby (172.16.100.11), gunicorn on :8001 as `nyayo.service` with four workers of four threads each (`gthread`), in a
conda env, next to OpenBench but sharing nothing. `nyayo-worker.service` runs `manage.py worker`, bound to
`nyayo.service` so it starts, stops and restarts with it, `Restart=always`, at low CPU and disk priority;
`nyayo-expire.timer` (04:30) and `nyayo-backup.timer` (05:00) run daily. `deploy/install-service.sh` installs the units
and `deploy/update.sh` pulls, installs, migrates, collects static files and restarts nyayo and the worker. The owner
runs `sudo` on bobby; changes there are proposed, not made. [deploy.md](deploy.md) has the install.

- **Game threads.** Each gunicorn worker lets at most `NYAYO_GAME_THREADS` (2) of its threads handle game requests
  (`PUT` and `PATCH` of a game, a batch upload) at once; one that finds them taken is `503` with `Retry-After: 1`
  before its body is read, and the producer sends it again. Match reports, pages and viewers' `GET`s never wait for a
  slot. The count is per process; raise it with the unit's `--threads`.
- **Settings**, from the environment or an untracked `.env`: `NYAYO_SECRET_KEY`, `NYAYO_DEBUG`, `NYAYO_ALLOWED_HOSTS`,
  `NYAYO_CSRF_TRUSTED_ORIGINS`, `NYAYO_SCRIPT_NAME` (for a path prefix behind a proxy), `NYAYO_DATA_DIR`,
  `NYAYO_WIP_DAYS`, `NYAYO_COMMITTED_DAYS`, `NYAYO_SILENT_AFTER`, `NYAYO_GAME_POLL_MS`, `NYAYO_GAME_THREADS`,
  `NYAYO_ARCHIVE_EVERY`, `NYAYO_BATCHES_DIR`, `NYAYO_BATCH_GAMES`, `NYAYO_SEAL_AFTER`, `NYAYO_READER`,
  `NYAYO_READER_SHA256`, `NYAYO_INDEX_DB`, `NYAYO_BACKUP_REPO`, `NYAYO_DATASETS_DIR` (position datasets' files, next to the batches by default), `NYAYO_BOOK_MIN_PLY` and `NYAYO_MIN_PLY_DEFAULT` (a dataset's first ply by book), `NYAYO_GITHUB_REPO` (the engine's repository, owner/name, for the dashboard's links), `NYAYO_TIME_ZONE` (the zone pages show times in, an IANA name) and `NYAYO_LOG_LEVEL`. A server error's traceback goes
  to the journal (`journalctl -u nyayo`); the worker's reasons are in `journalctl -u nyayo-worker`.
- **The share.** The batches are on the Synology's `nyayo-batches` folder over NFS 4.1, mounted on bobby at
  `/mnt/nyayo-batches` (`NYAYO_BATCHES_DIR=/mnt/nyayo-batches/batches`); a `RequiresMountsFor` drop-in on both units
  keeps nyayo from starting, and writing batches to bobby's own disk, before it is mounted. Its snapshots and backup are
  the Synology's.
- **The reader** is elephant's `phant-indexer` in `~/dev/nyayo-bin/`, pinned by its sha256.

State is the database, `archive/` (runs and matches, the source of truth), `games/` (the only copy of each live game's
PGN and its record), `uploads/` (uploads waiting for the worker), the batches (data to keep) and `index.sqlite3` (derived).

**Backup.** `manage.py backup` takes an online copy of the database into the data directory's `backup/` and, when
`NYAYO_BACKUP_REPO` names a checkout of a private git repository, mirrors `archive/` and `games/` into it with the
database as an SQL dump (`db.sql`: tokens, admin users and snapshots live only in the database), commits what changed
and pushes. `nyayo-backup.timer` runs it daily at 05:00. The batches are left out of the repo, being gigabytes: they
are the share's to back up. `uploads/` (an upload is its producer's to send again) and the index (derived) are left out
too. `manage.py restore_backup REPO` builds a fresh data directory from such a repository and refuses one that has
data. Not built: rebuilding the database from the archive alone.

## Interfaces

| Direction | What | Contract |
|:--|:--|:--|
| in | runs from `results.py` on developer machines | results schema v1 ([results-schema.md](results-schema.md)) |
| in | matches and their games from uwanja: reports (whole, or by changes with a seq), live games whole or by `PATCH` | match schema v1 ([match-schema.md](match-schema.md)) |
| in | the worker's calls: workloads asked for, steps, results, errors, games | the `client*` endpoints of OpenBench's Client (openbench_eg), above |
| in | SPRTs and fixed-games tests from OpenBench: the match whole, with `stats`; each workload's games as an upload | match schema v1, *OpenBench* |
| in | the game reader, a binary nyayo runs: PGN on standard input, one JSON line per game | `phant-indexer`'s entries, format 1 or 2 (`indexer/README.md` in [looak/elephant](https://github.com/looak/elephant)) |
| out | web pages for people | above |
| out | every stored game, filtered | `GET /api/games`, one bz2 stream (match schema, *Export*) |
| out | the data, to a private git repository | `manage.py backup`, above |
