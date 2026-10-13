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

A bare Django project that serves a placeholder page and the admin. Nothing is ingested yet. See
[CLAUDE.md](CLAUDE.md) for the design and the open decisions.

## Running it

```
python -m venv .venv
.venv/Scripts/python -m pip install -e .
cp .env.example .env
.venv/Scripts/python manage.py migrate
.venv/Scripts/python manage.py runserver 127.0.0.1:8001
```

Installing it on a server (bobby, next to OpenBench, as a systemd service in a conda env) is described in
[docs/deploy.md](docs/deploy.md).

## License

GPL-3.0, see [LICENSE](LICENSE).
