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

- `nyayo.service`, the site on port 8001, four sync workers: live game pages poll ten times a second and a gauntlet
  runner PUTs every move, so a couple of slow pages must not queue them;
- `nyayo-expire.timer`, which runs `manage.py expire_runs` daily at 04:30 through `nyayo-expire.service`. It deletes
  wip runs never promoted within 14 days and committed runs not published within 30 (`NYAYO_WIP_DAYS`,
  `NYAYO_COMMITTED_DAYS` in `.env`). Published runs are kept. `python manage.py expire_runs --dry-run` shows what
  it would delete.

A running match with no report for `NYAYO_SILENT_AFTER` minutes (30 by default, in `.env`) is shown as *silent* on
`/matches` and its page; nothing is stored, and its next report clears it.

Running the script again reinstalls the units, which is how a change to a unit file reaches the server. Follow the
log with `journalctl -u nyayo -f`.

### Updating

```bash
deploy/update.sh
```

It pulls, reinstalls the package, migrates the database, collects static files and restarts the service. No need to
activate the env first.

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
