# Running nyayo

nyayo is a Django app with a SQLite database. All state (the database and the raw results archive) lives in one data
directory, `./data` unless `NYAYO_DATA_DIR` says otherwise. Configuration comes from a `.env` file in the
repository root; `.env.example` lists every setting.

## Development on Windows

```powershell
git clone git@github.com:looak/nyayo.git
cd nyayo
python -m venv .venv
.venv\Scripts\python -m pip install -e .
Copy-Item .env.example .env        # NYAYO_DEBUG=true is enough locally
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py createsuperuser
.venv\Scripts\python manage.py runserver 127.0.0.1:8001
```

Open http://127.0.0.1:8001/ for the site and http://127.0.0.1:8001/admin/ for the admin.

## bobby

bobby (172.16.100.11, user `fischer`) also runs OpenBench on port 8000. nyayo runs next to it as its own service on
port 8001 under gunicorn, in its own conda env. They share nothing, so either can be restarted without touching the
other.

### First install

```bash
mkdir -p ~/dev && cd ~/dev
git clone https://github.com/looak/nyayo.git        # or git@github.com:... if bobby has a deploy key
cd nyayo
conda create -n nyayo python=3.13 -y
conda activate nyayo
pip install -e .
```

Create `~/dev/nyayo/.env`:

```bash
NYAYO_DEBUG=false
NYAYO_SECRET_KEY=<output of: python -c "import secrets; print(secrets.token_urlsafe(50))">
NYAYO_ALLOWED_HOSTS=172.16.100.11,bobby.home,localhost
NYAYO_CSRF_TRUSTED_ORIGINS=http://172.16.100.11:8001,http://bobby.home:8001
NYAYO_DATA_DIR=/home/fischer/dev/nyayo-data
```

Keeping the data directory outside the checkout means a fresh clone or `git clean` never touches it.

Then set up the database and static files, and try it by hand:

```bash
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
gunicorn nyayo.wsgi:application --bind 0.0.0.0:8001 --workers 2
```

Browse to http://172.16.100.11:8001/ and stop it with Ctrl-C.

### As a service

```bash
deploy/install-service.sh
```

It fills in the units in `deploy/` for this checkout, user and conda env (found through `conda info --base`; set
`NYAYO_CONDA_ENV` if the env isn't called `nyayo`), installs them with `sudo`, starts them and shows their status:

- `nyayo.service`, the site on port 8001, four workers of four threads each (`gthread`): live game pages poll ten times a second and a gauntlet
  runner PUTs every move, so a couple of slow pages must not queue them;
- `nyayo-worker.service`, nyayo's worker (`manage.py worker`): it reads uploads of finished games (OpenBench sends
  one per workload) into the matches' batches in the background, so the upload itself is answered at once. It is bound
  to `nyayo.service`, starting, stopping and restarting with it, `Restart=always`, at low CPU and disk priority, and
  looks for work every second; nobody runs it by hand. `python manage.py worker --once` does what is waiting and
  exits, and refuses to start while the service's worker runs (one worker at a time). Follow it with
  `journalctl -u nyayo-worker -f`; a refused upload's reason is there and in the admin (Matches, Uploads), where it can
  be read again;
- `nyayo-expire.timer`, which runs `manage.py expire_runs` daily at 04:30 through `nyayo-expire.service`. It deletes
  wip runs never promoted within 14 days and committed runs not published within 30 (`NYAYO_WIP_DAYS`,
  `NYAYO_COMMITTED_DAYS` in `.env`). Published runs are kept. `python manage.py expire_runs --dry-run` shows what
  it would delete.

- `nyayo-backup.timer`, which runs `manage.py backup` daily at 05:00 through `nyayo-backup.service`; see *Backup*.

A running match with no report for `NYAYO_SILENT_AFTER` minutes (30 by default, in `.env`) is shown as *silent* on
`/matches` and its page; nothing is stored, and its next report clears it.

Running the script again reinstalls the units, which is how a change to a unit file reaches the server, and how
`nyayo-worker.service` is installed the first time. Follow the
log with `journalctl -u nyayo -f`.

### Updating

```bash
deploy/update.sh
```

It pulls, reinstalls the package, migrates the database, collects static files and restarts the service. No need to
activate the env first.

## The game reader

The worker reads every game it keeps through a game reader: python-chess unless `.env` names elephant's
**phant-indexer**, a release binary pinned by its sha256. The worker checks the binary's hash and its `--version`
before it starts and refuses to start on a mismatch (the unit then fails and says why in
`journalctl -u nyayo-worker`), so a binary replaced under it is never run. Each batch and upload records the reader
that read it (`phant-indexer 1.3.1`, `python-chess 1.11.2`).

```bash
mkdir -p ~/dev/nyayo-bin
cp phant-indexer-1.3.1-linux-x86-64 ~/dev/nyayo-bin/ && chmod +x ~/dev/nyayo-bin/phant-indexer-1.3.1-linux-x86-64
sha256sum ~/dev/nyayo-bin/phant-indexer-1.3.1-linux-x86-64      # compare with the release's
```

and in `.env`:

```bash
NYAYO_READER=/home/fischer/dev/nyayo-bin/phant-indexer-1.3.1-linux-x86-64
NYAYO_READER_SHA256=<the release's sha256>
```

then `sudo systemctl restart nyayo-worker`. A new release is a new file and a new hash, never the old file replaced.

Measured on 2026-10-21 in WSL (one thread of a Ryzen 9 9900X) on a restore of bobby's 2026-10-20 backup, every
stored game read as one input per producer through nyayo's own interface (a pipe in, JSON lines out, parsed):

| Games | python-chess 1.11.2 | phant-indexer 1.3.1 (elephant#29, `40623e8`) |
|:--|--:|--:|
| OpenBench, 42,116 games, 155 MB | 75.3 s, 559 games/s | 2.01 s, 20,934 games/s |
| uwanja, 2,737 games, 59 MB (fastchess, PVs in the comments) | 6.8 s, 405 games/s | 0.26 s, 10,469 games/s |

They agree on every game: its place, offset and length, whether it read, its result, its plies and its tags (the
tags a PGN has; python-chess also fills in the seven-tag roster). bobby's CPU is slower than this one; the ratio,
about 37 to 1, is what carries over.

## The index

Every game kept as data has a card in `index.sqlite3` (see *Storage* in `match-schema.md`). The worker writes a
card as it appends a game. After a new release of the reader, or a change to what a card holds, run

```bash
python manage.py reindex --dry-run     # how many batches are out of date
python manage.py reindex               # at low priority, one batch at a time, safe to stop and start again
```

It visits only the batches whose cards were filled by an older reader or extractor version. `--rebuild` writes every
card again and is how a lost index is made again from the batches. `--only eval` runs one extractor.

## Backup

Everything nyayo knows is the database, `archive/` (the raw runs and matches) and `games/` (the only copy of each
live game). `manage.py backup` takes an online copy of the database into `backup/` in the data directory (never copy
the `.sqlite3` file while the service runs, it is in WAL mode) and, when `NYAYO_BACKUP_REPO` names a checkout, mirrors
`archive/` and `games/` into it with the database as an SQL dump (`db.sql`: text, so git stores deltas), commits what
changed and pushes. `nyayo-backup.timer` runs it daily; a failed push leaves the commit and fails the unit, so
`systemctl status nyayo-backup` and `journalctl -u nyayo-backup` say so.

Once, on bobby: make a private repository, clone it (`git clone git@github.com:<you>/nyayo-backup.git
~/dev/nyayo-backup`, with a key bobby may push with), set `NYAYO_BACKUP_REPO=/home/fischer/dev/nyayo-backup` in
`.env`, and run `python manage.py backup` by hand to see it work. The dump holds the machines' token hashes and the
admin users, hence private.

`games/batches/` (the game database: nyayo's 64-game batches of finished games, zstd) is not in the repo: it will be
gigabytes. Put it on the Synology's share and let the NAS's own backup cover it: mount the share on
bobby and set `NYAYO_BATCHES_DIR` to it in `.env` (it defaults to `games/batches` in the data directory). Batches are
never purged with the live games; `manage.py purge_batches --match ID` removes a match's by hand, and a match that has
batches can't be deleted in the admin until then. Two more things are left out on purpose: `uploads/` (an upload
waiting for the worker, OpenBench's to send again; it keeps the bytes in its own tars) and the index of the games,
`index.sqlite3` (`NYAYO_INDEX_DB`), which is derived from the batches. Keep the index on bobby's own disk, never on
the share: SQLite's locking isn't safe over SMB or NFS.

To restore on a new machine: install nyayo, set `NYAYO_DATA_DIR` to an empty directory, clone the backup repository,
stop nyayo, then `python manage.py restore_backup <path to the clone>`. It refuses a data directory that already has
a database, archive or games. Restore `games/batches/` from the share.

## When a page gives an error

A server error's traceback goes to the journal, with the time and the address that failed:

```bash
journalctl -u nyayo --since "15 min ago" | grep -B2 -A25 "ERROR"
journalctl -u nyayo -f            # follow it while you reproduce the error
```

The access log beside it (`"GET /matches HTTP/1.1" 500`) says which request and when; the lines above it say why.
From a client, `curl -i http://172.16.100.11:8001/matches` shows the status line and headers, and the browser's network
tab shows the same; neither shows the cause, which only the server knows. `NYAYO_LOG_LEVEL=DEBUG` in `.env` makes
nyayo's own messages chattier.

Two things to tell apart: **a 500** is Django failing on a request, and has a traceback; **a hang, a 502 or a dropped
connection** is the server not answering, and shows as `WORKER TIMEOUT` in the same journal. Workers are threaded
(`gthread`) so a browser holding a connection open and sending nothing doesn't tie one up; with the old sync workers
that produced a timeout every half hour.

## Machines and tokens

Every machine that pushes runs has its own token, so a run can be traced to the box that sent it. In the admin
(`/admin/`, Runs, Machines) add a machine; the token is shown once, at the top of the page, copy it then. To revoke
one untick *active*; the *new token* action replaces a lost one.

On the machine, in the Elephant checkout:

```sh
git config nyayo.url http://172.16.100.11:8001      # http://127.0.0.1:8000 for a local nyayo
git config nyayo.token <the token>
python test/results.py sync                         # uploads every run nyayo doesn't have yet
```

`test/smoke.sh` and Elephant's post-commit hook sync on their own after that. Deleting `logs/results/uploaded.json`
makes the next sync upload everything again.

## Behind a reverse proxy later

If nyayo moves behind nginx or Caddy at a path such as `https://bobby.home/nyayo`, set `NYAYO_SCRIPT_NAME=/nyayo`,
have the proxy strip that prefix before forwarding to port 8001, and add the new origin to
`NYAYO_CSRF_TRUSTED_ORIGINS`.
