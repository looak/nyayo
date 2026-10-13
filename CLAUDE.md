# nyayo

An orchestrator and reporting platform for chess engine smoke tests. It starts runs, collects their results into a
database and shows how every metric moves over time, commit by commit. The engine it was built for is Elephant
(github.com/looak/elephant), but engine specifics belong in configuration, not code.

## What nyayo is not

- **Not a worker.** It never plays games or runs the engine. Work happens on GitHub Actions, OpenBench or a test
  machine; nyayo dispatches it and ingests what comes back.
- **Not a live viewer.** That is [bwawa](https://github.com/looak/bwawa) (sibling checkout at `../bwawa`). nyayo links to
  a bwawa instance for live games instead of rebuilding that.

## Settled decisions

- **Python.** bwawa is imported as a package (`pip install -e ../bwawa`) for `bwawa.sprt` (OpenBench's pentanomial LLR,
  Wald projection, normalized Elo), the Elo calculation, PGN and fastchess parsing and the OpenBench page scraper.
  Reuse these, don't copy them. If something in bwawa needs changing to be reusable, that's a bwawa commit.
- **One results contract.** Every run, wherever it executes, produces one `smoke-results.json`: commit SHA, host,
  timestamps, and a list of checks `{name, status, metrics{}, artifacts[]}`. That file is the only interface between
  workers and nyayo. Define and version it (a `schema_version` field) before anything else; the script that writes it
  lives in the engine's repo, next to the CI workflow.
- **Pull, don't push, for GitHub-hosted runners.** They can't reach the home LAN, so nyayo polls the GitHub API for new
  commits and finished workflow runs and downloads the results artifact. No public webhook endpoint.
- **SQLite** to start: one user, one writer. Metrics go in a narrow table (run, name, value, unit) so a new metric needs
  no migration.
- **Dispatchers:** GitHub Actions `workflow_dispatch` (extended smoke), OpenBench (submitting its test form; it has no
  JSON API, read results by scraping the test page like bwawa does), optionally ssh to the test machine for a quick
  fastchess run.

## Reporting, where the value is

- Timelines per metric across commits: bench node count, NPS, perft, crashes, illegal moves, time losses,
  tactical-suite score, quick Elo vs a baseline with its error bars.
- A page per commit with every check.
- Regression flags: bench signature changed without a `Bench:` line in the commit message, NPS drop beyond noise, a
  new crash or time loss, suite score drop. Possibly posted back to GitHub as a commit status.

## Open decisions (ask the owner, don't guess)

1. Which checks are "smoke" (every push) and which are "extended" (nightly / on demand).
2. GitHub-hosted runners (nyayo pulls results) or a self-hosted runner on hunt (it could POST results straight to nyayo).
3. Django (ORM, migrations, admin, same stack as OpenBench) or aiohttp + SQLite (same stack as bwawa).

## Environment

- **Deployment host: bobby** (172.16.100.11, user `fischer`), the same machine as the owner's OpenBench. nyayo is served
  at `https://bobby.home/nyayo`, so the app must work under a path prefix (`FORCE_SCRIPT_NAME` in Django, a prefix
  setting otherwise).
- OpenBench on bobby is Django `runserver` on port 8000 as `openbench.service`, at the root path. Serving nyayo next to
  it needs a reverse proxy (nginx or Caddy) in front of both. Not set up yet. The owner edits server files and runs
  `sudo` there; propose changes, don't make them.
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
