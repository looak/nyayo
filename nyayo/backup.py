"""Backing nyayo up and restoring from it.

State is the SQLite database, `archive/` (raw runs and matches) and `games/` (the only copy of each live game's PGN).
`backup()` takes an online copy of the database (never the file itself while the service runs, it's in WAL mode), and,
when a git repo is configured, mirrors the archive and games into it with the database as an SQL dump (text, so git
stores the changes as deltas rather than a new binary every day), commits what changed and pushes.

`games/batches/` is left out of the repo: batches are data to keep, gigabytes of bz2, and belong on the Synology's
share under its own backup. Whatever the share is mounted as, it isn't in the git repo.

`restore()` makes a fresh data directory from such a repo: the database from the dump, the archive and the games.
"""

import filecmp
import shutil
import sqlite3
import subprocess
from pathlib import Path

DUMP = "db.sql"
TREES = ("archive", "games")
# in the data directory, not copied into the repo
SKIP = {("games", "batches")}


class BackupError(Exception):
    pass


def online_copy(source, destination):
    """The database as it is now, consistent, while others may be writing to it."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    reading = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    writing = sqlite3.connect(destination)
    try:
        reading.backup(writing)
    finally:
        writing.close()
        reading.close()


def write_dump(database, destination):
    connection = sqlite3.connect(database)
    try:
        lines = list(connection.iterdump())
    finally:
        connection.close()
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def mirror(source, destination, skip=()):
    """Makes destination hold what source holds: changed files copied, files gone from source removed. Returns the
    number of files copied or removed. `skip` is relative paths (as tuples of parts) left out."""
    changed = 0
    wanted = set()
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(relative.parts[:len(s)] == s for s in skip):
            continue
        if path.is_dir():
            (destination / relative).mkdir(parents=True, exist_ok=True)
            continue
        wanted.add(relative)
        target = destination / relative
        if target.exists() and filecmp.cmp(path, target, shallow=False):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        changed += 1
    if destination.exists():
        for path in sorted(destination.rglob("*"), reverse=True):
            relative = path.relative_to(destination)
            if path.is_file() and relative not in wanted:
                path.unlink()
                changed += 1
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
    return changed


def git(repo, *args):
    try:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    except FileNotFoundError as error:
        raise BackupError("git isn't installed") from error
    if done.returncode != 0:
        raise BackupError(f"git {' '.join(args)} failed: {(done.stderr or done.stdout).strip()}")
    return done.stdout.strip()


def backup(data_dir, database, repo=None, push=True, message="nyayo backup"):
    """Returns a short account of what was done. Raises BackupError for a repo that isn't one or a push that fails
    (the commit stays, the next run pushes it)."""
    data_dir, database = Path(data_dir), Path(database)
    copy = data_dir / "backup" / database.name
    online_copy(database, copy)
    done = [f"database copied to {copy}"]
    if repo is None:
        return done
    repo = Path(repo)
    if not (repo / ".git").exists():
        raise BackupError(f"{repo} isn't a git checkout; clone the private backup repository there first")
    write_dump(copy, repo / DUMP)
    files = 0
    for tree in TREES:
        if (data_dir / tree).is_dir():
            files += mirror(data_dir / tree, repo / tree, skip=[s[1:] for s in SKIP if s[0] == tree])
    git(repo, "add", "--all")
    if git(repo, "status", "--porcelain"):
        git(repo, "commit", "-m", message)
        done.append(f"committed to {repo} ({files} files changed in archive and games)")
    else:
        done.append("nothing changed since the last backup")
    if push and "origin" in git(repo, "remote").split():
        branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        git(repo, "push", "origin", branch)
        done.append(f"pushed {branch}")
    return done


def restore(repo, data_dir, database):
    """A fresh data directory from a backup repo. Refuses to touch one that has a database, an archive or games."""
    repo, data_dir, database = Path(repo), Path(data_dir), Path(database)
    if not (repo / DUMP).is_file():
        raise BackupError(f"{repo / DUMP} not found; is that a nyayo backup?")
    if database.exists() or any((data_dir / tree).exists() and any((data_dir / tree).iterdir()) for tree in TREES):
        raise BackupError(f"{data_dir} already has data; restore into an empty data directory")
    data_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.executescript((repo / DUMP).read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()
    for tree in TREES:
        if (repo / tree).is_dir():
            mirror(repo / tree, data_dir / tree)
    return [f"database restored to {database}", f"archive and games restored under {data_dir}",
            "games/batches is not in the backup repo; restore it from the share it lives on"]
