# nyayo

An orchestrator, reporting platform and live viewer for chess engine testing. It starts runs, collects their results
into a database and shows how every check, metric and test position moves over time, commit by commit. It also
receives matches (gauntlets first, later SPRTs) and their games as they are played, and shows them live. The engine it
was built for is Elephant (github.com/looak/elephant, sibling checkout at `../elephant`), but engine specifics belong
in configuration, not code.

## What nyayo is not

- **Not a worker.** It never plays games or runs the engine. Work happens on GitHub Actions or the owner's dev
  machine; nyayo dispatches it and ingests what comes back.
- **Not an Elo or speed tracker.** Elo testing lives on OpenBench and stays there; NPS is measured elsewhere. nyayo
  tracks deterministic checks: determinism (bench signature), crashes and tactical suites (WAC, Arasan, STS).
- **Not a game runner.** Matches are played by [uwanja](https://github.com/looak/uwanja) (the gauntlet runner, sibling
  checkout at `../uwanja`) driving fastchess, or by OpenBench; they report to nyayo, which only stores and shows them.

## Settled decisions

- **nyayo is a live viewer.** It replaces [bwawa](https://github.com/looak/bwawa) (sibling checkout at `../bwawa`),
  which is being retired (`../ramani/plans/retire-bwawa.md`): the SPRT maths and progress view come here, the game
  tracker and fastchess log parsing go to uwanja. Don't link out to bwawa; port what's needed, bwawa is not a
  dependency.
- **Matches are a second contract, next to runs:** `docs/match-schema.md`, its own schema version 1, approved by the
  owner (draft in `../ramani/plans/match-schema.md`). `PUT /api/matches/{id}` with the whole match document, same
  tokens and responses as runs; ids are `[A-Za-z0-9._-]`, at most 128 characters. No match state is final, counts
  only grow, everything else but state and timestamps is fixed once received (`409` naming the field). Results schema
  v1 is untouched. Counts are from the subject's side; statistics (Elo, LLR, performance rating) are computed here,
  never trusted from the producer.
- **Games stream in whole.** `PUT /api/matches/{id}/games/{game}` with the whole PGN so far
  (`application/x-chess-pgn`), re-sent after every move; a game with a final result is sealed and further PUTs are
  `409`. `{game}` is uwanja's game number, stable across a resume. PGNs are files under `games/<match id>/<game>.pgn`,
  apart from results so `manage.py purge_games` can remove them; the index row is touched only when result or
  termination changes, not on every move, to keep SQLite writes few. Viewers poll; no websockets. Live pages need no
  login, as the run pages. Gunicorn needs 4 or more sync workers for this; propose it in `docs/deploy.md`.
- **Gauntlet slice first.** Match document, per-opponent counts, games and live game pages. SPRT, OpenBench reports
  and the progress projection come after. Opponent ratings are trusted as sent and stored with the match.

- **Python and Django** (ORM, migrations, the admin for browsing and purging runs, same stack as OpenBench). PGN
  parsing uses python-chess.
- **One results contract, owned by nyayo:** `docs/results-schema.md`, schema version 1. A run is one JSON document:
  the run (id, state, commit or tree, branch, ancestry, host, build, timestamps) and its checks, each with params,
  metrics and per-position cases. It changes only by bumping `schema_version`, agreed with the owner. Elephant writes
  it with `test/results.py` (see its `test/RESULTS.md`).
- **Two ways in, one ingest.** `nyayo.runs.ingest.ingest()` archives the raw run, then indexes it. Push: machines on
  the LAN `PUT /api/runs/{id}` with the whole run (usually gzipped) and a per-machine bearer token made and revoked
  in the admin; `201` created, `200` updated, `401` bad token, `400`/`409` with a short plain-text reason the client
  shows. Pull (not built yet): GitHub-hosted runners can't reach the LAN, so nyayo will poll for finished workflow
  runs and ingest their `results-<os>` artifacts through the same function. No public webhook endpoint.
- **Runs are wip, committed or published**, and only move forward. wip: a dirty tree, `commit` null, `tree` is the
  tree the working copy would commit as. committed: a commit with exactly that tree exists, possibly only locally.
  published: the commit is on a remote branch; only these are permanent history, and CI runs start here. The client
  promotes runs; nyayo doesn't match trees. Under one run id only `state` (forward), `commit` (null to a sha, once),
  `committed_at`, `published_at` and `ancestors` may change; anything else is `409` naming the field.
- **Expiry** (`manage.py expire_runs`, a daily timer): wip runs never promoted go after `NYAYO_WIP_DAYS` (14),
  committed runs not published after `NYAYO_COMMITTED_DAYS` (30). Published runs are never expired.
- **Comparability.** Checks are comparable when id, version, limit, threads, hash, experiment and build (compiler,
  config, assertions, exceptions, bits) match; `per_file` only picks positions. A position is compared when its id
  and its EPD file's content hash match. `deterministic: false` checks (multi threaded) compare on totals only. A
  missing check or case is shown as missing, never as a pass.
- **Baselines follow ancestry, not recency.** Per check, the newest comparable run on the nearest commit in the run's
  `ancestors` (`git rev-list --topo-order -n 300`, its own commit first), published before committed, never wip
  unless picked by hand. So parallel branches are never compared by accident. The second comparison is the previous
  run on the same branch, which may be wip. Manually picked A/B pairs are allowed and labelled when unrelated.
- **Raw results are the source of truth.** Every ingested run is archived unchanged as `archive/<id>.json` (with a
  `.meta.json` naming the machine), replaced on update; the database is an index that can be rebuilt from it. The
  archive, not the `.db`, is what gets backed up off-machine, e.g. pushed to a private git repo.
- **SQLite** to start: one user, one writer. Metrics go in a narrow table (run, name, value, unit) so a new metric needs
  no migration.
- **Dispatch:** GitHub Actions `workflow_dispatch` for the extended smoke run (about 30 minutes: determinism, crashes,
  tactics). Scheduled and push-triggered runs also start from GitHub itself, and nyayo ingests any run that produced
  the artifact, so it catches up after downtime.

## Reporting, where the value is

- Timelines per check and metric across commits: bench node count, perft, crashes, tactical-suite scores.
- History per test position: when `WAC.297` last passed, which commit broke it.
- A page per commit with every check, and A/B between two runs down to the individual position.
- Regression flags: bench signature changed without a `Bench:` line in the commit message, a new crash, a position
  that newly fails. Possibly posted back to GitHub as a commit status.

## Open decisions (ask the owner, don't guess)

1. Exactly which suites and node budgets go in smoke (every push) versus extended (nightly / on demand).
2. Where crashes are detected: suites under a sanitizer build, or separate fast self-play games.
3. Which builds must agree for the determinism check (compilers, OSes, release vs sanitizer).

## Specs

What this repo does is specified here, in `docs/spec.md`: Purpose, Scope, Current and Interfaces. **Keep it true in
the same pull request as the code**: a change that alters a command, an endpoint, a schema, a file format or anything
the spec describes updates the spec in that pull request. When the code and the spec disagree, the code wins and the
spec is fixed.

Where this repo is going lives in ramani (github.com/looak/ramani, private, sibling checkout at `../ramani`): the
README for the system and the contracts table, `nyayo.md` for this repo's Direction (planned work, open decisions),
and `plans/` for work spanning repos. **Read ramani, never edit it**, nor any other sibling checkout. A
pull request that does a plan's step says `Closes looak/<repo>#<sub-issue>`. When it settles one of ramani's open
decisions, say so in the final report. Contracts this repo owns (`docs/match-schema.md` and `docs/results-schema.md`)
are changed only here, by agreement with the owner.

## Environment

- **Deployment host: bobby** (172.16.100.11, user `fischer`), an Ubuntu VM with 8 GB on a Synology DS923+, the same
  machine as the owner's OpenBench. nyayo runs independently under gunicorn on its own port (8001) as `nyayo.service`,
  in a conda env. It should still work under a path prefix (`NYAYO_SCRIPT_NAME`, i.e. `FORCE_SCRIPT_NAME`) in case a
  reverse proxy goes in front later. See `docs/deploy.md`.
- OpenBench on bobby is Django `runserver` on port 8000 as `openbench.service`, at the root path. The owner edits
  server files and runs `sudo` there; propose changes, don't make them.
- bobby runs a newer OpenBench than the local `D:\dev\openbench_eg` checkout: engine configs live in its database, not
  in `Engines/*.json`. OpenBench bounds are normalized Elo.
- **Test machine: hunt** (172.16.100.15, user `loodinek`, 32 cores, ssh key auth works), the OpenBench worker.
- From the owner's Windows PC, `*.home` names only resolve while the hosts file lists them (the work VPN blocks the home
  DNS server). Prefer IP addresses in scripts.
- Development happens on Windows: use `.venv/Scripts/python`.

## Conventions

- Small commits. Messages are a short subject, then a paragraph or two of plain prose, no bullet lists. No AI
  attribution or Co-Authored-By lines.
- Push only when the owner asks.
- Secrets (GitHub token, OpenBench credentials) come from the environment or an untracked `.env`, never the repo.
- Keep the README current as features land.
