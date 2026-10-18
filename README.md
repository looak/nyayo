# nyayo

*Swahili for footprints, the trail the herd leaves behind.*

Smoke tests and their history for a chess engine. nyayo notices new commits, starts test runs on GitHub Actions,
OpenBench or a test machine, collects what they report and keeps it in a database, so a regression shows up as a
step in a timeline and the commit that caused it is one click away.

It plays no games itself. The runs happen elsewhere and each one hands back a single results file; nyayo dispatches,
stores and reports.

It is also the live viewer for matches: gauntlets from [uwanja](https://github.com/looak/uwanja) and SPRTs from
OpenBench report to it, and every game of a gauntlet can be watched move by move. It replaces
[bwawa](https://github.com/looak/bwawa).

## Status

nyayo receives smoke runs pushed from developer machines and shows them:

- **Runs**, newest first, filtered by branch, state (wip, committed, published) and tier (smoke, extended). Each is
  named by its branch and commit and sums up its checks: the worst verdict, positions solved over all of them and a
  marker per check, plus how long the run took.
- **A run**, every check against its baseline: the newest comparable run on the nearest commit it descends from.
  Each check shows its verdict, its settings, when it ran and how long it took, its score as a bar split into
  positions that pass, newly pass, newly fail and fail (with the suite's minimum and the baseline's score marked), and
  a grid of every position linking to its history. Then the positions that newly fail or pass, STS points that moved, and positions that found another
  move.
- **Compare** any two runs, or a run against its baselines or the previous run on its branch. Checks searched with
  different settings are listed as not comparable, never counted as passing.
- **A position** (`/case/epd.wac_new/WAC.297`) on a board with arrows for the expected move (and moves to avoid) and a
  link to lichess.org's analysis board, then
  every attempt along a branch's history with its time, the move played in SAN and how long the search took: since
  when it fails, when it last passed.

- **Matches** (`/matches`), gauntlets and SPRTs with their state, games and Elo. A gauntlet shows each opponent's
  score, pentanomial and Elo (as cards or a list, with a table to copy), and every game on a live page with board,
  clocks, evaluation and the engines' figures. An SPRT shows its LLR against its bounds, the producer's Elo, Elo and
  LLR over games, and an estimate of the games and time left to a bound. A running match that has stopped reporting is
  marked silent.

Runs arrive as `PUT /api/runs/{id}` with a per-machine token; the format and the rules are in
[docs/results-schema.md](docs/results-schema.md); matches as `PUT /api/matches/{id}`, in
[docs/match-schema.md](docs/match-schema.md). Pulling CI runs from GitHub comes next. See
[CLAUDE.md](CLAUDE.md) for the design and the open decisions.

## Running it

```
python -m venv .venv
.venv/Scripts/python -m pip install -e .
cp .env.example .env
.venv/Scripts/python manage.py migrate
.venv/Scripts/python manage.py createsuperuser
.venv/Scripts/python manage.py runserver 127.0.0.1:8000
.venv/Scripts/python manage.py test nyayo
```

Add a machine in the admin to get a push token. Installing on a server (bobby, next to OpenBench, as a systemd
service in a conda env) and connecting Elephant are described in [docs/deploy.md](docs/deploy.md).

## License

GPL-3.0, see [LICENSE](LICENSE).
