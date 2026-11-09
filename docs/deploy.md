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

bobby (172.16.100.11, user `fischer`) runs one nyayo, as its own service on port 8001 under gunicorn, in its own conda
env. OpenBench's server is not run any more (see *The lab*); the workers talk to nyayo. Where things are on bobby:

| What | Where |
|:--|:--|
| Checkout, on `main` | `~/dev/nyayo-2` |
| Conda env | `nyayo2` |
| Data directory (`NYAYO_DATA_DIR`) | `~/dev/nyayo-2-data` |
| Batches (`NYAYO_BATCHES_DIR`) | `/mnt/nyayo-batches/batches-2` (NFS, the Synology's `/volume2/nyayo-batches`) |
| Datasets (`NYAYO_DATASETS_DIR`) | `/mnt/nyayo-batches/datasets-2` |
| Backup repository (`NYAYO_BACKUP_REPO`) | `~/dev/nyayo-backups`, a git repository |
| Units | `nyayo`, `nyayo-worker`, `nyayo-expire.timer`, `nyayo-backup.timer`, installed from `~/dev/nyayo-2` |

The names with a `2` are leftovers of how it got there (*How nyayo 2.0 replaced the old nyayo*); nothing else depends on
them. A fresh install elsewhere can use plain `nyayo`, `nyayo-data` and so on, and the rest of this section is written that way.

### First install

```bash
mkdir -p ~/dev && cd ~/dev
git clone https://github.com/looak/nyayo.git        # or git@github.com:... if the machine has a deploy key
cd nyayo                                            # <checkout>: ~/dev/nyayo-2 on bobby
conda create -n nyayo python=3.13 -y                # <env>: nyayo2 on bobby
conda activate nyayo
pip install -e .
```

Create `<checkout>/.env`:

```bash
NYAYO_DEBUG=false
NYAYO_SECRET_KEY=<output of: python -c "import secrets; print(secrets.token_urlsafe(50))">
NYAYO_ALLOWED_HOSTS=172.16.100.11,bobby.home,localhost
NYAYO_CSRF_TRUSTED_ORIGINS=http://172.16.100.11:8001,http://bobby.home:8001
NYAYO_DATA_DIR=/home/fischer/dev/nyayo-data                 # bobby: /home/fischer/dev/nyayo-2-data
NYAYO_BATCHES_DIR=/mnt/nyayo-batches/batches                # bobby: /mnt/nyayo-batches/batches-2
NYAYO_DATASETS_DIR=/mnt/nyayo-batches/datasets              # bobby: /mnt/nyayo-batches/datasets-2
NYAYO_BACKUP_REPO=/home/fischer/dev/nyayo-backups
NYAYO_TIME_ZONE=America/Vancouver
```

The data directory is outside the checkout, so a fresh clone or `git clean` never touches it. The batches and datasets
are on the share, which the NAS backs up (see *Backup*); the datasets default to a directory beside the batches, so
name both. **Two nyayos must never share batches**: deleting a match with its games removes its batch files. `.env.example` lists the rest. The backup repository is made once, see *Backup*; leave `NYAYO_BACKUP_REPO` out until it is.

Tell the scripts which env this checkout uses, so a plain `deploy/update.sh` finds it. **`deploy/env.sh` takes the env
from `NYAYO_CONDA_ENV` (`nyayo` when unset), never from the conda env that happens to be active**, so with an env of
another name and no line below, `update.sh` and `install-service.sh` use the wrong one (or fail). Put it first in
`~/.bashrc`, so it is set before anything that returns early for a non-interactive shell:

```bash
export NYAYO_CONDA_ENV=nyayo        # nyayo2 on bobby; the name of <env> above
```

Then set up the database, static files and fastchess, and try it by hand:

```bash
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
python manage.py fetch_fastchess v1.8.2-alpha       # see The lab, Fastchess from the server
gunicorn nyayo.wsgi:application --bind 0.0.0.0:8001 --workers 2
```

Browse to http://172.16.100.11:8001/ and stop it with Ctrl-C.

With `NYAYO_DEBUG` off the static files are served with a hash in their name (`/static/lab/create_test.06376fbc7d38.js`), made by `collectstatic`, so a browser never keeps an old script or style sheet after an update; run `collectstatic` after every update (`deploy/update.sh` does). A page that names a file `collectstatic` didn't find is an error, not a broken link.

### As a service

```bash
deploy/install-service.sh
```

It fills in the units in `deploy/` for this checkout, user and conda env (found through `conda info --base`, from
`NYAYO_CONDA_ENV`, `nyayo` when unset), installs them with `sudo`, starts them and shows their status:

- `nyayo.service`, the site on port 8001, four workers of four threads each (`gthread`): live game pages poll ten times a second and a gauntlet
  runner PUTs every move, so a couple of slow pages must not queue them. Game requests (a game's moves, a batch of
  games) may take at most `NYAYO_GAME_THREADS` (2) of each worker's threads; the others are kept for match reports and
  pages, so a flood of games never holds up a test's status (see *Game threads*);
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

The pages show times in `NYAYO_TIME_ZONE` (an IANA name, UTC when unset), and the top bar says which; on bobby set
`NYAYO_TIME_ZONE=America/Vancouver` in `.env`. Times are stored in UTC whatever it is, so it can change at any time.

A test in `failed_prechecks` shows that it deletes itself `NYAYO_FAILED_PRECHECKS_HOURS` (48 by default, in `.env`) hours
after it ended, and `nyayo-worker.service` deletes it then, with its archive: it looks at start and then every
`NYAYO_EXPIRY_EVERY` seconds (3600), so it goes within an hour of its time. The page and the worker read the same setting;
`nyayo-expire.timer` has nothing to do with it. A match with games, and any other state, is never deleted.

A running match with no report for `NYAYO_SILENT_AFTER` minutes (30 by default, in `.env`) is shown as *silent* on
the Tests list and its page; nothing is stored, and its next report clears it.

A match whose producer reports by changes (uwanja, once it sends `report.seq`) is archived whole at most every
`NYAYO_ARCHIVE_EVERY` seconds while it runs (60 by default) and when it ends, instead of on every report; the archive
is still the whole truth the database can be rebuilt from.

Running the script again reinstalls the units, which is how a change to a unit file reaches the server, and how
`nyayo-worker.service` is installed the first time. Follow the
log with `journalctl -u nyayo -f`.

### Updating

```bash
deploy/update.sh
```

It pulls (fast-forward only), reinstalls the package, migrates the database, collects static files and restarts `nyayo` and
`nyayo-worker`. No need to activate the env first, and it does not look at the active one: it uses the env that
`NYAYO_CONDA_ENV` names (`nyayo` when unset; `nyayo2` on bobby, set in `~/.bashrc`, so a plain `deploy/update.sh` from
`~/dev/nyayo-2` is right). `NYAYO_UNITS` names other units to restart, if there ever is a second nyayo on a machine.

## How nyayo 2.0 replaced the old nyayo

History, not instructions: it was done once and cannot be repeated.

On 2026-11-05 nyayo 2.0 replaced the old nyayo on bobby. It was first run **beside** the old one as a trial, with its own
checkout, env, data, batches (a copy) and port (8002), while the old one went on unchanged on 8001. Then, in one sitting, the
old units were stopped, the data was copied again from the old nyayo, and 2.0's units took port 8001 and the names `nyayo`,
`nyayo-worker` and the two timers. The old checkout `~/dev/nyayo` and the conda env `nyayo` were then deleted, on the owner's
word, so there is no going back to the old nyayo. `manage.py rename_matches` was run the same day (*Renaming the matches*): every
old `openbench-openbench-N` is now `sprt-N`.

The scripts that did it (`make-env.sh`, `copy-live-data.sh`, `install-side-by-side.sh`) were removed with it; they are in
the git history if a similar move is ever needed.

Where things live now is in the table under *bobby*. Two things came out of it that still matter:

- **The data copy must finish before the service is installed.** The units start nyayo on whatever
  data directory there is, an empty one included. Run out of order, the install started 2.0 on an empty database. Whenever a
  data directory is filled from elsewhere (a copy from another machine, a restore), finish that first and install or start
  the services only afterwards.
- **`NYAYO_CONDA_ENV` decides the env**, not the active one (*First install*). On bobby it is `nyayo2`, set first in
  `~/.bashrc`.

### Leftovers on bobby

Of the old nyayo these are still on bobby, unused, until the owner removes them: `~/dev/nyayo-data` (the old data
directory, about 435 MB), `/mnt/nyayo-batches/batches` and `/mnt/nyayo-batches/datasets` (the old batches and datasets, on the
share). Nothing reads them: 2.0 uses `batches-2` and `datasets-2`. They are also the last copy of the old nyayo's data as
it was before the switch, so this is for when the owner is sure; **removal cannot be undone**. Look first, then remove:

```bash
du -sh ~/dev/nyayo-data /mnt/nyayo-batches/batches /mnt/nyayo-batches/datasets
rm -rf ~/dev/nyayo-data /mnt/nyayo-batches/batches /mnt/nyayo-batches/datasets
```

Check that the paths are those and not the `-2` ones, with `grep -E '^NYAYO_(DATA|BATCHES|DATASETS)_DIR' ~/dev/nyayo-2/.env`.

## The lab: OpenBench's workers

nyayo is OpenBench's server: it makes the tests, hands the workers their work, and takes their results.
The worker that plays is OpenBench's own `Client/`, which nyayo carries (`nyayo/client/`, from looak/openbench_eg `6a072fa`) and hands out at `/client.zip`, with the registration of *Workers register themselves* below: no username and no password. OpenBench's own server is not run any more: nyayo
is what the workers talk to (`/client.zip`, client 56).

**What is new in the environment**

- Python 3.12 or later in the env (`pyproject.toml` says `>=3.12`; the first install above makes the env with 3.13). Check
  with `python --version` in the env: an older one makes `pip install -e .` fail with "requires a different Python".
- `deploy/update.sh` runs `pip install -e .`, which brings `scipy` and `numpy` (the SPRT maths), both with wheels for Linux.
- A test's match is `<mode>-<test id>` (`sprt-1000`, `games-1001`, `tune-1002`, `datagen-1003`); nothing is set for it. The first
  migration of the lab starts test ids at the next thousand above the highest `openbench-<anything>-<N>` there was, so a test
  of nyayo's never took a number OpenBench used.
- The rest, all optional, is in `.env.example`: `NYAYO_GITHUB_TOKEN` for the create page's lookups, and what the workers
  are told to fetch (`NYAYO_CLIENT_*`, `NYAYO_FASTCHESS_*`; the defaults are right).

**The workers must reach nyayo.** A worker talks to `http://172.16.100.11:8001/` (the paths `clientGetWorkload/`,
`clientReportStep/` and so on, at the root), so port 8001 must be open to hunt and `NYAYO_ALLOWED_HOSTS` must name the
address the worker uses. From the worker's machine:

```bash
curl -s http://172.16.100.11:8001/clientVersionRef/
```

answers `{"client_version": 56, "client_url": "http://172.16.100.11:8001/client.zip", "client_sha256": "..."}`
(`clientVersionRef` needs nothing, so a client can always update itself, and from nyayo, not GitHub). There is no path prefix for a worker to get wrong:
with `NYAYO_SCRIPT_NAME` set it would have to be part of `-S`.

**Workers register themselves.** There is no worker user and no password. A worker says hello (`POST /clientHello/`) with
its name (`-I`, else the host's) and is given a token, once, which it keeps in `worker.token` beside `client.py` (an update of
the client does not remove it); nyayo keeps only the token's hash. It then describes itself, and is **pending** until the
owner approves it on Machines (*Approve*), or it came with a join code (`--join CODE`, made on Machines: one worker, once, an
hour to two weeks, shown once). Every call after the hello is the worker's session; a restart makes a new session on the same
row, so a worker is one row under Lab, Machines however often it starts. The owner can *Turn away* a worker (it is told to
wait, and its name is refused a new token), and *Disable* an approved one (it finishes a workload it has and is given no new
one) and *Enable* it again; each is an event, with who did it. A pending worker is told what to describe, then to wait, and
is refused everything else: no test, no source, no network. At most 20 workers may be pending, and one that has played
nothing and has not been approved in 7 days is removed. The token goes over plain HTTP, as a password did: fine on the LAN, and
HTTPS beyond it. An approved worker runs what the server sends it, so a client is pointed only at a server its owner trusts.

**People.** The owner is the superuser (`python manage.py createsuperuser`): the one who signs in to `/sign-in`, makes and
approves tests, approves workers, and uses `/admin/`. That is the only user to make.

**What it seeds.** The first migration of the lab adds the Elephant engine (source `https://github.com/looak/elephant`,
built with `ob_build`, compilers `g` and `clang`, presets STC, LTC, SMP STC and SMP LTC with time controls such as
`8.0+0.08`) and OpenBench's twelve books. They are under Lab, Engine configs and Books in the admin (the Engines and Books
pages in the sidebar are not built yet).

**What a worker needs** (hunt): Python 3 with the packages in the client's `requirements.txt` (`requests`, `psutil`,
`py-cpuinfo`, and optionally `zstandard`), and access to `github.com` and `raw.githubusercontent.com`, from where it downloads
the engine's source as an archive and the books (the client itself comes from nyayo). To build Elephant it needs `make` and a C and C++ compiler. When it has
described itself the server judges what it can build: the engine's compilers, `g` and `clang`, are `g++` or `gcc`, and `clang`
or `clang++` (a `>=` minimum is kept), and `make` is needed too. The tools the worker is asked about are `gcc`, `g++`,
`clang`, `clang++`, `dotnet`, `rustc`, `cargo`, `python3` and `make`; only C and C++ are scheduled for now, so an engine that
builds with Rust or C# is handed to no worker. The page of a worker says what it found and what it plays; one that plays
nothing says what is missing (`Missing ['g', 'clang']`, `Missing make`, a missing CPU flag such as `POPCNT`, an operating
system the engine is not built for). A test no worker can play stays *waiting for a worker*. It is got and started with the commands on nyayo's `/worker` page (no sign-in), which have the server's own address in them:

```bash
curl -fLo client.zip http://172.16.100.11:8001/client.zip
python3 -m zipfile -e client.zip nyayo-worker
python3 -m pip install -r nyayo-worker/requirements.txt
python3 nyayo-worker/client.py --server http://172.16.100.11:8001 -T <threads> -N <sockets> -I hunt
```

(`--server`, or `-S`, is the whole address, no path; it may instead be in `OPENBENCH_SERVER`. Add `--join CODE` to be approved on
hello.) A worker that already has a client directory from OpenBench's fork needs only `-S` changed and the credentials taken off:
it updates itself from nyayo when the version asks, and keeps its `worker.token`. The client updates itself from nyayo, never
from GitHub: raise its version (`CLIENT_VERSION` in `nyayo/client/worker.py`) in a release, and every worker follows.
Its first run says *waiting for approval*; the page of Machines shows it as *Waiting for approval* with *Approve* and *Turn
away*. A worker plays for one server at a time: stop it before it is pointed elsewhere.

**Fastchess from the server.** nyayo can hand a worker the official fastchess release's file for its system, so a worker
needs no compiler for it: `python manage.py fetch_fastchess <tag>` (`v1.8.1-alpha` or `1.8.1-alpha`) downloads that release
of `Disservin/fastchess` (AndyGrant's has no commits of its own over it) into `DATA_DIR/fastchess/<tag>/`, with a
`manifest.json` of each file's sha256. A release file is an archive of the source tree and the executable; the archive's checksum is
checked against GitHub's, and the executable alone is kept and served (`fastchess-linux-x86-64`, `fastchess-windows-x86-64.exe`),
with its own sha256. A file that is no archive, or holds no `fastchess`, is served as it came. `clientMatchRunnerVersionRef` then names the file for the system and
architecture a worker asks with (`os_name`, `arch`), with its sha256, if its release is at least `NYAYO_FASTCHESS_MIN_VERSION`;
the worker downloads it from `/clientFastchess/<tag>/<file>`, checks the sha256 and runs its version, and builds from source
where there is no file or it will not run. Any worker may fetch it, approved or not. Run the command once per release, on
bobby, as the user the service runs as; nothing restarts.

**Elephant's `main` has no `Bench:` line** in its commit message, which the create page needs to know a commit's bench.
Under *Advanced* on Create test, type the base's bench (and the dev's, if its commit has none either) in *Base bench*:
the node count `bench` prints for that commit. The page says "Unable to parse a Bench" until it is.

**The smoke test** (`tools/smoke/run.py`; its docstring has the setup) is how to see all of this work before a real test:
nyayo as deployed, nyayo's own client downloaded from `/client.zip` and started with no
username and no password (no checkout of openbench_eg), a toy engine and a GitHub of its own, tests made through the real create page, the engine's failures
staged. It checks hello, pending (told to wait, nothing played), describe, approve and play, disable and enable, a join code, a
turned-away worker refused by name, a restart keeping its row and its games, a worker updating itself from nyayo, and fastchess from the server. Linux or WSL,
`make` and `g++`, about three to four minutes.

## Game threads

Producers send games far more often than anything else: uwanja PUTs a live game after every move, OpenBench a batch
per workload. Each gunicorn worker lets at most `NYAYO_GAME_THREADS` of its threads handle them at once (2 of 4 by
default, set in `.env`); a game request that finds them taken is answered `503` with `Retry-After: 1` before its body
is read, and the producer sends it again. A match report (`PUT /api/matches/{id}`) and the pages never wait for a game
slot, so with four workers there are always eight threads for them. The count is per worker, being per process; keep
it below `--threads` in `deploy/nyayo.service`, and raise both together if games lag behind while the CPU is idle.

The `503`s show in the access log, not as errors. Under a flood of games they are many; what matters is that
`PUT /api/matches/...` keeps answering `200` quickly.

Tried under gunicorn 26 with the service's settings (four workers of four threads, pinned to two cores) and 32
clients sending 100 KB games at 100 KB/s, as over a slow link: without the cap a match report waited 0.6 s for a
thread (median), with it 5 ms. With games sent at full speed the CPU is the limit rather than the threads, and the
cap makes little difference (a median of 30 to 45 ms either way).

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

## Position datasets

The worker draws position datasets for the tuner (the Exports page, `POST /api/exports`; `docs/match-schema.md`,
Position datasets), after everything else it has to do, a batch a turn. It needs phant-indexer as the reader, in a
release with `--draw` (1.3.2 on, looak/elephant#34; its `--version` may add `positions 2`, which nyayo accepts): pin it as for reading games (`NYAYO_READER`, `NYAYO_READER_SHA256`)
and restart the worker. The files go to `NYAYO_DATASETS_DIR`, by default `datasets/` next to the batches, on the
share; a dataset of 2026-10's 45,000 games is under 10 MB.

To start one from another machine, with that machine's token:

```bash
curl -X POST -H "Authorization: Bearer <token>" -d '{"note": "retune the king table"}' http://172.16.100.11:8001/api/exports
curl http://172.16.100.11:8001/api/exports/<id>     # state, progress, and the file's URL once done
```

## The index

Every game kept as data has a card in `index.sqlite3` (see *Storage* in `match-schema.md`). The worker writes a
card as it appends a game. After a new release of the reader, or a change to what a card holds, run

```bash
python manage.py reindex --dry-run     # how many batches are out of date
python manage.py reindex               # at low priority, one batch at a time, safe to stop and start again
```

It visits only the batches whose cards were filled by an older reader or extractor version. `--rebuild` writes every
card again and is how a lost index is made again from the batches. `--only eval` runs one extractor.

The played extractor's version 2 dates a game that has only a `Date` tag (OpenBench's, today) by when its upload
arrived, not midnight, so a workload sorts among the games of its day. After updating to it, restamp the stored cards
once with `python manage.py reindex --only played`.

## Migrating the stored games into batches

Once, after the worker and the reader are installed (see *The game reader*) and `NYAYO_BATCHES_DIR` is on the share:

```bash
python manage.py backup                      # first, by hand
python manage.py keep_games --dry-run        # reads every finished single game, prints what it would keep
python manage.py keep_games                  # does it, and checks itself
```

Each finished single game is read, appended as it came to its match's batches with its id kept (batches are sealed
at 64 games, a match that has ended has its last one sealed short), and indexed with how it ended. A game that
carries no ending is deduced against its match's adjudication settings and marked inferred. Nothing in `games/` or
`archive/` is changed or deleted. It then checks, and fails naming the problem unless: each match's games in batches
equal its finished games; the export before and after holds the same games; the reader and python-chess read the
batches' games the same. Run again it finds nothing to do. Games still being played stay where they are.

Only after it passes, `python manage.py purge_games --match ID` (or `--older-than DAYS`) removes the single files of
games in sealed batches; it keeps back any that aren't, and counts them. It takes about four minutes for 45,000 games.

## Backup

Everything nyayo knows is the database, `archive/` (the raw runs and matches) and `games/` (the only copy of each
live game). `manage.py backup` takes an online copy of the database into `backup/` in the data directory (never copy
the `.sqlite3` file while the service runs, it is in WAL mode) and, when `NYAYO_BACKUP_REPO` names a checkout, mirrors
`archive/` and `games/` into it with the database as an SQL dump (`db.sql`: text, so git stores deltas), commits what
changed and pushes. `nyayo-backup.timer` runs it daily; a failed push leaves the commit and fails the unit, so
`systemctl status nyayo-backup` and `journalctl -u nyayo-backup` say so.

Once, on a new install: make a private repository, clone it (`git clone git@github.com:<you>/nyayo-backups.git
~/dev/nyayo-backups`, with a key the machine may push with), set `NYAYO_BACKUP_REPO=/home/fischer/dev/nyayo-backups` in
`.env`, and run `python manage.py backup` by hand to see it work. On bobby that is done: `~/dev/nyayo-backups` is the
repository, and `nyayo-backup.timer` is installed with the other units. The dump holds the machines' token hashes and the
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

## Renaming the matches

`python manage.py rename_matches` turns the `openbench-openbench-N` matches of an OpenBench-era database into `sprt-N` (`games-N`
and `tune-N` for a lab test of that mode). **It has been run on bobby (2026-11-05) and does not need running again**: every old match
there is already `sprt-N`, and a second run finds nothing to do. What follows is what it does and how to run it, for a
fresh copy of old data (a restore of a backup from before the rename, say). Old links, `/matches/openbench-openbench-1000` and everything under it, keep working: they
answer `301` to `/matches/sprt-1000`.

What holds a match's id, and so what it moves: the `Match` row (its primary key) and every row that refers to it (`ScheduleEntry`,
`MatchResult`, `MatchSnapshot`, `Upload`, `Batch`, `Game`), one transaction per match; the datasets that name it
(`manifest[].match`, `selection.matches`); `archive/matches/<id>.json`, with `match.id` rewritten, and `<id>.meta.json`; the
directories `games/<id>/`, `<NYAYO_BATCHES_DIR>/<id>/` and `uploads/<id>/`. The index database (`index.sqlite3`) names
batches by row number and holds no match id. Tombstones keep their ids (a link to a deleted match still says so). It writes a
`RenamedMatch` row per match, which is what the redirect reads and what a second run finds leftover files by. Gauntlet ids (uuids)
are not of the form and are not touched. The command refuses, before changing anything, a new id that is taken (a match, a
tombstone, an existing file or directory that differs) or wanted by two matches, and a match of a kind with no rule (only
`sprt` and `games`) that is not a lab test.

Order, with the services that write stopped (the worker and the web process both write matches; the expire timer deletes them):

```bash
sudo systemctl stop nyayo-expire.timer nyayo-backup.timer nyayo nyayo-worker
cd <checkout> && conda activate <env>              # ~/dev/nyayo-2 and nyayo2 on bobby
python manage.py backup                            # a fresh backup, then look at it: the database and the archive
git pull --ff-only && python -m pip install --quiet -e . && python manage.py migrate   # the table of renames
python manage.py rename_matches --dry-run          # every rename and move, one line each; nothing changes
python manage.py rename_matches                    # for real
python manage.py rename_matches --dry-run          # again: "0 match(es) would be renamed, 0 file(s)..."
sudo systemctl start nyayo nyayo-worker
curl -sI http://172.16.100.11:8001/matches/openbench-openbench-1000 | head -3   # 301, Location: /matches/sprt-1000
sudo systemctl start nyayo-expire.timer nyayo-backup.timer
```

If it stops (a crash, Ctrl-C, a full disk), run it again: the database part is a transaction per match, and the files are found
from the `RenamedMatch` rows, so it finishes what is left and does nothing twice. A refusal changes nothing; fix what it names.
It rewrites the archive documents and moves files, so the old names are not kept: the backup before it is the way back (restore the
database and the archive from it, and put the directories back with `mv`).

The next `manage.py backup` sees the renames as files gone and files new. `nyayo-backups` (git) records the archive and `games/`
moves as renames of the same content (the archive documents differ by their id), so it is a one-time commit and not the history
again, and the old names stay in the earlier commits. `games/batches/` and `uploads/` are not in the repo; the Synology's own backup
of the share sees the renamed `batches/<id>/` directories as new and, unless it deduplicates, uploads them once more. Take that
backup before and after, and check the free space on the share.

## When a page gives an error

A server error's traceback goes to the journal, with the time and the address that failed:

```bash
journalctl -u nyayo --since "15 min ago" | grep -B2 -A25 "ERROR"
journalctl -u nyayo -f            # follow it while you reproduce the error
```

The access log beside it (`"GET /tests HTTP/1.1" 500`) says which request and when; the lines above it say why.
From a client, `curl -i http://172.16.100.11:8001/tests` shows the status line and headers, and the browser's network
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
git config nyayo.url http://172.16.100.11:8001      # http://127.0.0.1:8001 for a local nyayo
git config nyayo.token <the token>
python test/results.py sync                         # uploads every run nyayo doesn't have yet
```

`test/smoke.sh` and Elephant's post-commit hook sync on their own after that. Deleting `logs/results/uploaded.json`
makes the next sync upload everything again.

## Behind a reverse proxy later

If nyayo moves behind nginx or Caddy at a path such as `https://bobby.home/nyayo`, set `NYAYO_SCRIPT_NAME=/nyayo`,
have the proxy strip that prefix before forwarding to port 8001, and add the new origin to
`NYAYO_CSRF_TRUSTED_ORIGINS`.

The *Response* on the Machines pages is the kernel's round-trip estimate of the connection a worker's call arrived on
(`nyayo/lab/rtt.py`). Behind a proxy that connection is the proxy's, so the figure would be the proxy to nyayo, a few
microseconds on one machine, and not the worker's network at all. Until it is measured another way (the proxy passing a
measured time on in a header, which nyayo does not read), expect it to say nothing useful there. It is also empty under
`runserver` and on Windows, and for a box that is offline.
