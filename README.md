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

Not started. See [CLAUDE.md](CLAUDE.md) for the design and the open decisions.

## License

GPL-3.0, see [LICENSE](LICENSE).
