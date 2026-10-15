# Results schema, version 1

A run is one JSON document: the run's identity and where it came from, then its checks, each with metrics and,
for position suites, a result per position (a *case*). It's the only interface between whatever runs the tests and
nyayo. nyayo owns this schema: it changes only by bumping `schema_version`, agreed with the owner, and nyayo refuses
any other version.

Elephant writes it with `test/results.py`; its `test/RESULTS.md` describes how runs are recorded and synced on that
side. This version was taken from it at Elephant's `herd-smoke-report` branch (37ae5eb).

## Uploading

```
PUT /api/runs/{run.id}
Authorization: Bearer <token>
Content-Type: application/json
Content-Encoding: gzip            (optional)

<the whole run>
```

| Response | When |
|:--|:--|
| `201 created` | a new run id |
| `200 updated` | a run id nyayo has: the same version again, or a promotion |
| `400 <reason>` | not a valid version 1 run, or `run.id` differs from the url's |
| `401` | missing, unknown or revoked token |
| `409 <reason>` | a change nyayo doesn't allow under this id; the reason names the field |

Response bodies are a short plain-text reason meant to be shown to the user. Each machine has its own token, made
and revoked in nyayo's admin. The client re-sends the whole run whenever it changes; a state change is the same call
again.

GitHub-hosted CI runs can't reach nyayo; nyayo will pull them from the workflow's artifacts instead and ingest them
the same way.

## States and what may change

`run.state` is `wip`, `committed` or `published`.

- `wip`: recorded on a dirty working tree. `commit` is null; `tree` is the git tree the working copy would commit
  as (everything added, untracked files included) and `base_commit` is HEAD. It's compared with history but isn't
  part of it.
- `committed`: a commit was made whose tree is exactly the run's tree, and the client filled in `commit`. The commit
  may exist only on the developer's machine; an amend or rebase can orphan it.
- `published`: the commit is on a remote branch. Only published runs are history for good. CI runs are published
  from the start.

The client promotes runs, nyayo doesn't match trees itself. Under one run id only these may change between uploads:

- `run.state`, forward only: wip, then committed, then published;
- `run.commit`, from null to a sha, once;
- `run.committed_at` and `run.published_at`;
- `run.ancestors`, recomputed from the new commit on promotion.

Everything else (tree, branch, build, host, the other timestamps, every check and case) is fixed once a run is
recorded, and a change is refused with `409`.

wip runs never promoted expire 14 days after `recorded_at`; committed runs not published expire 30 days after
`committed_at` (or `recorded_at` without one). Published runs are kept.

## Comparing

**Comparable checks.** Two checks searched the same way when they have the same `id`, `version`, `params.limit`,
`params.threads`, `params.hash_mb` and `params.experiment`, and the same build (`compiler`, `config`, `assertions`,
`exceptions`, `bits`, from the check's `build` or else the run's). A run can hold two checks with the same id and
different params, WAC with 1 and with 4 threads, so `id` alone doesn't identify a check; two checks with the same key
in one run are refused. `per_file` only selects positions and isn't part of the key.

**Comparable positions.** A case is compared when its `id` and its file's content hash,
`params.file_sha256[case.file]`, both match. An EPD file edited under the same ids starts a new history.

**Multi-threaded checks**, `deterministic: false`, are compared on totals only, never per position.

**Baselines.** For each check, the newest run with a comparable check on the nearest commit in the run's
`ancestors`, starting with its own commit; published before committed; never a wip run unless picked by hand. The
second comparison is the previous run on the same branch, which may be wip.

## Format

```jsonc
{
  "schema_version": 1,
  "run": {
    "id": "fc505f6c-dde8-455b-bc05-a50813801536",   // uuid made when recorded, the upload key
    "kind": "smoke",                       // smoke | extended
    "source": "dev",                       // dev | ci
    "trigger": "local",                    // local | push | schedule | request
    "request_id": null,
    "state": "committed",                  // wip | committed | published
    "commit": "d695c0979c...",             // null while wip; a full sha otherwise
    "base_commit": "148709c3c9...",        // HEAD when the run was made
    "tree": "3477723319c8...",             // the working copy's tree
    "branch": "herd-smoke-report",
    "ancestors": ["d695c0979c...", "..."], // git rev-list --topo-order -n 300, the commit (or base) first
    "version": "1.0.0-herd.10",
    "started_at": "2026-10-14T01:57:01Z", "finished_at": "...", "recorded_at": "...",   // recorded_at required
    "committed_at": "...", "published_at": "...",                                       // optional
    "host": { "name": "BABYLON", "os": "Windows 11", "cpu": "...", "logical_cores": 24 },
    "build": { "compiler": "msvc 195136260", "config": "release", "assertions": true, "exceptions": true, "bits": 64 },
    "ci": { "workflow_run_id": 123, "url": "..." }   // CI only
  },
  "checks": [
    {
      "id": "epd.wac_new", "version": 1, "name": "Smoke_WinAtChess",
      "status": "pass",                    // the harness: pass ran to completion | error | skipped
      "deterministic": true,
      "params": { "limit": { "type": "nodes", "value": 150000 }, "threads": 1, "hash_mb": 64, "per_file": 0,
                  "files": ["wac_new.epd"], "file_sha256": { "wac_new.epd": "..." }, "experiment": false },
      "metrics": { "solved": { "value": 260 }, "total": { "value": 300 },
                   "points": { "value": 1707 }, "max_points": { "value": 3250 },   // scored suites (STS)
                   "duration": { "value": 14.2, "unit": "s" } },
      "engine_verdict": { "baseline": 260, "minimum": 252, "repeatable": true, "result": "pass" },
      "started_at": "2026-10-14T01:57:08Z", "finished_at": "2026-10-14T01:57:22Z",          // optional
      "build": { "...": "as run.build" },
      "cases": [
        { "id": "WAC.001", "file": "wac_new.epd", "fen": "...", "status": "pass",   // pass | fail | error
          "expected": ["Qg6"], "avoid": [], "got": "g3g6",
          "points": 10, "max_points": 10,                                             // scored suites
          "score": 23996, "mate": true, "nodes": 141388, "time_ms": 54, "pv": "g3g6 f7g6 e5g6" }
      ]
    }
  ]
}
```

A case passes when the move played is one of `expected` (bm, SAN) and none of `avoid` (am); `got` is the move played
in coordinate notation. For STS it earns the points of its c0 list, and `status` still says whether it was the best
move. `error` is a case the harness couldn't run as written, a FEN or a bm/am the parsers rejected. `engine_verdict`
is the suite's own baseline and minimum, for information: whether a change is a regression is worked out from history.

**Timing** is shown, never compared: the run's and each check's `started_at` and `finished_at`, the `duration`
metric and each case's `time_ms` depend on what else the machine is doing. They're there to spot a suite that suddenly
takes five times as long. Timestamps are ISO 8601 with a timezone.

Metrics are free-form: any name with a numeric `value` and an optional `unit` is stored, so a new metric needs no
change in nyayo. Checks without positions (bench, unit tests) leave `cases` out.
