# nyayo

*Swahili for footprints, the trail the herd leaves behind.*

Smoke tests and their history for a chess engine. nyayo notices new commits, starts test runs on GitHub Actions,
OpenBench or a test machine, collects what they report and keeps it in a database, so a regression shows up as a
step in a timeline and the commit that caused it is one click away.

It plays no games itself. The runs happen elsewhere and each one hands back a single results file; nyayo dispatches,
stores and reports.

Live games are watched with [bwawa](https://github.com/looak/bwawa), which nyayo links to and borrows its SPRT and
Elo maths from.

## Status

nyayo receives smoke runs pushed from developer machines and shows them:

- **Runs**, newest first, filtered by branch and state (wip, committed, published).
- **A run**, every check against its baseline: the newest comparable run on the nearest commit it descends from.
  Positions that newly fail or pass, STS points that moved, and positions that found another move.
- **Compare** any two runs, or a run against its baselines or the previous run on its branch. Checks searched with
  different settings are listed as not comparable, never counted as passing.
- **A position** (`/case/epd.wac_new/WAC.297`) on a board with arrows for the expected move (and moves to avoid), then
  every attempt along a branch's history with its time and the move played in SAN: since when it fails, when it last
  passed.

Runs arrive as `PUT /api/runs/{id}` with a per-machine token; the format and the rules are in
[docs/results-schema.md](docs/results-schema.md). Pulling CI runs from GitHub comes next. See
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
