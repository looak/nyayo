# nyayo

An orchestrator and reporting platform for chess engine smoke tests. It starts runs, collects their results into a
database and shows how every check, metric and test position moves over time, commit by commit. The engine it was built for is Elephant
(github.com/looak/elephant), but engine specifics belong in configuration, not code.

## What nyayo is not

- **Not a worker.** It never plays games or runs the engine. Work happens on GitHub Actions or the owner's dev
  machine; nyayo dispatches it and ingests what comes back.
- **Not an Elo or speed tracker.** Elo testing lives on OpenBench and stays there; NPS is measured elsewhere. nyayo
  tracks deterministic checks: determinism (bench signature), crashes and tactical suites (WAC, Arasan, STS).
- **Not a live viewer.** That is [bwawa](https://github.com/looak/bwawa) (sibling checkout at `../bwawa`). nyayo links to
  a bwawa instance for live games instead of rebuilding that.

## Settled decisions

- **Python and Django** (ORM, migrations, the admin for browsing and purging runs, same stack as OpenBench). If
  something from bwawa is needed (PGN or fastchess parsing), import it as a package rather than copying it; bwawa has
  no `pyproject.toml` yet, so making it installable is a bwawa commit.
- **One results contract.** Every run, wherever it executes, produces one `smoke-results.json`: a writer-generated run
  UUID, source (`ci` or `dev`), commit SHA plus a dirty flag and diff hash, host, build, timestamps, and a list of
  checks `{id, version, status, params{}, metrics{}, cases[], artifacts[]}`. Cases are per-position results (`WAC.297`
  pass/fail, an optional STS `score`). That file is the only interface between workers and nyayo. Define and version
  it (a `schema_version` field) before anything else; the script that writes it lives in the engine's repo, next to
  the CI workflow.
- **Two ways in, one ingest.** Push: anything on the LAN (the owner's dev machine, hunt) POSTs the file to
  `/api/runs` with a per-machine bearer token. Pull: GitHub-hosted runners can't reach the home LAN, so nyayo polls
  the GitHub API for finished workflow runs and downloads the results artifact; no public webhook endpoint. Both paths
  call the same ingest function. The run UUID makes ingest idempotent, so a dev script can spool files and retry.
- **Dev runs are second-class in history.** They appear on commit pages and in A/B comparisons but are off the main
  timelines by default, and may have shorter retention. Their commit may exist only locally.
- **Comparability.** Compare checks, not suites: two results are comparable only when check id, version and params
  (including the suite file's hash) match. Tactical suites run at fixed nodes on one thread so results are identical
  on any machine. A missing check or case is shown as missing, never as a pass.
- **Raw results are the source of truth.** Every ingested file is archived unchanged; the database is an index that
  can be rebuilt from the archive. The archive (small JSON, not the `.db`) is what gets backed up off-machine, e.g.
  pushed to a private git repo.
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
4. One push token per machine or one shared.

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
