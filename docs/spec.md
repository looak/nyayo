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
- **No state is final**: any state may go back to `running` (OpenBench restarts tests). `finished_at` is cleared and
  set again.
- **What may change under one id**: state, timestamps, the counts, the producer's LLR, result and `stats`, the machines
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

### Matches: SPRT viewer

A match with an `sprt` block gets **its own page**: an SPRT is a test of one change, not a field of opponents, so it
doesn't share the gauntlet's. Everything on it is the producer's; nyayo computes no SPRT. The top card has the games
played, the Elo with its error (the producer's, else nyayo's from the counts), the LLR with its bounds, the time
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

`/matches` shows a card for each match being played right now, above the list (games, Elo, how it is going and when
it will be done, and the boards of up to six games in play; a silent match is left out), and colours the list's score
and Elo. Every list pages 64 to a page. `manage.py seed_demo` (debug mode only) makes six simulated SPRTs and five
simulated gauntlets (running, finished, stopped with games cut off, silent, just started), with their games, to polish
the pages on.

### Pages

Every page sits in one frame, in Pi-hole's dark AdminLTE look: a sidebar and a top bar. The sidebar has a status block
(whether the worker holds its lock, the game reader, uploads waiting or refused, whether the batches' directory is
there and how much it holds) and the links, grouped as Main (Dashboard), Testing (Matches with the number running, Runs, Compare),
Catalogue (Games with the number kept, Uploads in the admin) and System (Admin); the page's own link is marked. The
top bar says where the page is, names the host and the time zone the pages' times are in (`NYAYO_TIME_ZONE`, UTC by default; times are stored in UTC). The status is worked out only when a page's frame is drawn, never
for the game page's polled fragment. The site has one colour scheme, dark.

| URL | Shows |
|:--|:--|
| `/` | four Pi-hole tiles: the game database (its size on disk, database, index and batches, the games it keeps and how many the index has carded, uploads waiting or refused), what is being played (the matches running and games in play, or with none, the match played last), the latest SPRT (the last to reach a verdict, else the newest: its Elo, LLR, and the crashes, time losses and illegal moves of the engine under test, red when there were any) and GitHub activity (the commits pushed in the last 7 days that have a run in nyayo, read from the published runs, on how many branches, and the latest push; linked to the repository on GitHub when `NYAYO_GITHUB_REPO` names it); the games kept by the day they were played over the last 53 weeks, as GitHub draws a year of commits (Monday first, days in the site's zone); two donuts of the kept games, how they ended and how they went for the engine under test; the running matches' cards with their boards in play; the latest matches; and the book positions most games started from, with the engine's score from each and a link to their games. Its figures over the kept games are SQL over the index's cards, about 0.4 s for 45,000. The home page; the sidebar's brand links to it |
| `/runs` | runs, newest first, filtered by branch, state and tier; each with its worst verdict, solved totals, a marker per check, duration |
| `/runs/{id}` | every check against its baseline: verdict, settings, when & how long, a score bar (pass, newly pass, newly fail, fail; minimum & baseline marked), a grid of positions; then what changed |
| `/compare` | any two runs, or a run against its baselines or its branch's previous run; incomparable checks listed as such |
| `/case/{check_id}/{case_id}` | one position on a board with expected & avoided moves, a lichess analysis link, and every attempt along a branch's history |
| `/matches` | matches, newest first, filtered by kind, state (or silent) and engine; games, score and Elo (an SPRT's is its producer's) |
| `/matches/{id}` | a head with the match's state and runner, Pi-hole tiles of the figures that matter (a gauntlet: played, performance, score, time to go; an SPRT: games, Elo, LLR, time to go), the settings, an opponent per card or per row of a list (the choice is remembered in a cookie): counts, score, Elo ± error, pentanomial, rating, faults; overall with the performance rating and a table to copy; an opponent can be left out of the figures from the page; a small board for every game in play, each a link to its game and drawn in the browser by chessground (`matches/minis.js`); the SPRT card when present; the games stored as data (and the uploads waiting or refused), with a link to their export; and the games, kept ones included: playing, cut off, scheduled, finished. Refreshes while the match runs. A match that isn't running never shows a game as playing |
| `/matches/{id}/games/{game}` | a large board with a bar and a clock per engine, an eval bar from white's side, a card per engine (score, depth, time, nodes, nps, PV), the moves in a scrolling list, and graphs per metric with a line per engine; stepping through the game by click or arrow keys. The board is lichess's chessground (9.1.1, GPL-3.0, vendored under `nyayo/matches/static/vendor/`), drawn in the browser from every position the page embeds, so a step shows at once and a move animates; the rest of the page then follows from the server. A game is read from its file, or from its batch once it is kept |
| `/games` | Pi-hole's query log: a filter bar that opens, and every game kept as data, newest first, 64 to a page, its row tinted by a win or a loss for the engine under test: when it was played (a time only where the game has one), its id (a link to its page), white and black (from the card's colour and the match's engines), its moves, result (and win, draw or loss for the engine under test), how it ended (deduced endings marked), the book position it started from with a link to the other games from it, and the match it came from. `?match=`, `?result=` (win, draw, loss), `?ended=` (a rule such as `checkmate` or `repetition`, or another ending such as `resignation` or `time_forfeit`), `?moves_from=` and `?moves_to=` (whole moves) and `?opening=` (16 hex digits) filter it, all together; a bad `ended`, moves or opening is `400`. It reads a page of cards from the index and fills in matches and engines for those 64 rows, so it takes about 0.2 s wherever it is in the list, and it shows a game once the worker has put it away. There are no opening names: the books start 16 plies in, and lichess's table names 0.2% of the games |
| `/admin/` | machines & tokens, browsing and purging runs and matches; uploads (with the reason a refused one was refused, and a way to read it again) and batches, read-only |
| `/healthz` | liveness |

The game page is **live**: it asks every 100 ms (`NYAYO_GAME_POLL_MS`) whether the game changed, sending the version it
shows, and the server answers an empty `204` from a file stat and one row query, so a poll with nothing new is nearly
free. The clocks are reconstructed from the time control and each move's time (exact where a move says `tl=`) and run
down locally between posts; polling pauses in a hidden tab. A change still redraws the page's whole game body; the
per-ply record a `PATCH` writes is not read by the pages yet.

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
  `NYAYO_READER_SHA256`, `NYAYO_INDEX_DB`, `NYAYO_BACKUP_REPO`, `NYAYO_GITHUB_REPO` (the engine's repository, owner/name, for the dashboard's links), `NYAYO_TIME_ZONE` (the zone pages show times in, an IANA name) and `NYAYO_LOG_LEVEL`. A server error's traceback goes
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
| in | SPRTs and fixed-games tests from OpenBench: the match whole, with `stats`; each workload's games as an upload | match schema v1, *OpenBench* |
| in | the game reader, a binary nyayo runs: PGN on standard input, one JSON line per game | `phant-indexer`'s entries, format 1 or 2 (`indexer/README.md` in [looak/elephant](https://github.com/looak/elephant)) |
| out | web pages for people | above |
| out | every stored game, filtered | `GET /api/games`, one bz2 stream (match schema, *Export*) |
| out | the data, to a private git repository | `manage.py backup`, above |
