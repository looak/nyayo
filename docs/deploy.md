# Running nyayo

nyayo is a Django app with a SQLite database. All state (the database and, later, the raw results archive) lives in
one data directory, `./data` unless `NYAYO_DATA_DIR` says otherwise. Configuration comes from a `.env` file in the
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

It fills in `deploy/nyayo.service` for this checkout, user and conda env (found through `conda info --base`; set
`NYAYO_CONDA_ENV` if the env isn't called `nyayo`), installs it with `sudo`, starts it and shows its status. Running it
again reinstalls the unit, which is how a change to the unit file reaches the server. Follow the log with
`journalctl -u nyayo -f`.

### Updating

```bash
deploy/update.sh
```

It pulls, reinstalls the package, migrates the database, collects static files and restarts the service. No need to
activate the env first.

## Behind a reverse proxy later

If nyayo moves behind nginx or Caddy at a path such as `https://bobby.home/nyayo`, set `NYAYO_SCRIPT_NAME=/nyayo`,
have the proxy strip that prefix before forwarding to port 8001, and add the new origin to
`NYAYO_CSRF_TRUSTED_ORIGINS`.
