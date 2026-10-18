"""Backing up to a git repository and restoring from it."""

import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from nyayo import backup

IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com"}


def run(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


class BackupCase(SimpleTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        patcher = mock.patch.dict(os.environ, IDENTITY)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.data = self.root / "data"
        (self.data / "archive" / "matches").mkdir(parents=True)
        (self.data / "games" / "m1").mkdir(parents=True)
        (self.data / "games" / "batches" / "m1").mkdir(parents=True)
        (self.data / "archive" / "run-1.json").write_text('{"run": 1}')
        (self.data / "archive" / "matches" / "m1.json").write_text('{"match": 1}')
        (self.data / "games" / "m1" / "1.pgn").write_text("1. e4 *")
        (self.data / "games" / "batches" / "m1" / "a.pgn.bz2").write_bytes(b"batch")
        self.database = self.data / "nyayo.sqlite3"
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE token (name TEXT, secret TEXT)")
        connection.execute("INSERT INTO token VALUES ('hunt', 'hash-1')")
        connection.commit()
        self.connection = connection  # stays open: a service writing while the backup runs
        self.addCleanup(connection.close)

        self.repo = self.root / "backup-repo"
        self.remote = self.root / "remote.git"
        run("git", "init", "--bare", "-b", "main", str(self.remote))
        run("git", "clone", str(self.remote), str(self.repo))
        run("git", "checkout", "-b", "main", cwd=self.repo)


class Backup(BackupCase):
    def test_a_local_copy_is_made_without_a_repo(self):
        done = backup.backup(self.data, self.database)
        copy = sqlite3.connect(self.data / "backup" / "nyayo.sqlite3")
        self.assertEqual(copy.execute("SELECT name FROM token").fetchall(), [("hunt",)])
        copy.close()
        self.assertEqual(len(done), 1)

    def test_the_copy_is_consistent_while_the_database_is_open_for_writing(self):
        self.connection.execute("INSERT INTO token VALUES ('bobby', 'hash-2')")
        self.connection.commit()
        backup.backup(self.data, self.database)
        copy = sqlite3.connect(self.data / "backup" / "nyayo.sqlite3")
        self.assertEqual(copy.execute("SELECT count(*) FROM token").fetchone(), (2,))
        copy.close()

    def test_the_repo_gets_the_archive_games_and_a_dump_but_not_batches(self):
        done = backup.backup(self.data, self.database, repo=self.repo)
        self.assertTrue((self.repo / "archive" / "run-1.json").is_file())
        self.assertTrue((self.repo / "archive" / "matches" / "m1.json").is_file())
        self.assertTrue((self.repo / "games" / "m1" / "1.pgn").is_file())
        self.assertFalse((self.repo / "games" / "batches").exists())
        self.assertIn("hash-1", (self.repo / "db.sql").read_text())
        self.assertTrue(any("committed" in line for line in done))
        self.assertTrue(any("pushed main" in line for line in done))
        log = subprocess.run(["git", "-C", str(self.remote), "log", "--oneline"], capture_output=True, text=True)
        self.assertEqual(len(log.stdout.splitlines()), 1)

    def test_an_unchanged_backup_commits_nothing(self):
        backup.backup(self.data, self.database, repo=self.repo)
        done = backup.backup(self.data, self.database, repo=self.repo)
        self.assertIn("nothing changed since the last backup", done)
        log = subprocess.run(["git", "-C", str(self.repo), "log", "--oneline"], capture_output=True, text=True)
        self.assertEqual(len(log.stdout.splitlines()), 1)

    def test_changes_and_removals_follow(self):
        backup.backup(self.data, self.database, repo=self.repo)
        (self.data / "archive" / "run-1.json").write_text('{"run": 2}')
        (self.data / "games" / "m1" / "1.pgn").unlink()
        (self.data / "games" / "m1").rmdir()
        backup.backup(self.data, self.database, repo=self.repo)
        self.assertEqual((self.repo / "archive" / "run-1.json").read_text(), '{"run": 2}')
        self.assertFalse((self.repo / "games" / "m1").exists())

    def test_no_push_commits_only(self):
        done = backup.backup(self.data, self.database, repo=self.repo, push=False)
        self.assertFalse(any("pushed" in line for line in done))

    def test_a_folder_that_is_not_a_repo_is_refused(self):
        with self.assertRaises(backup.BackupError):
            backup.backup(self.data, self.database, repo=self.root)

    def test_a_failing_push_is_reported_and_the_commit_stays(self):
        run("git", "remote", "set-url", "origin", str(self.root / "nowhere.git"), cwd=self.repo)
        with self.assertRaisesRegex(backup.BackupError, "push"):
            backup.backup(self.data, self.database, repo=self.repo)
        log = subprocess.run(["git", "-C", str(self.repo), "log", "--oneline"], capture_output=True, text=True)
        self.assertEqual(len(log.stdout.splitlines()), 1)


class Restore(BackupCase):
    def test_a_restore_into_a_fresh_directory_has_everything(self):
        self.connection.execute("INSERT INTO token VALUES ('it''s', 'multi\nline')")
        self.connection.commit()
        backup.backup(self.data, self.database, repo=self.repo)
        # a fresh clone, as on a new machine
        clone = self.root / "clone"
        run("git", "clone", str(self.remote), str(clone))
        fresh = self.root / "fresh"
        done = backup.restore(clone, fresh, fresh / "nyayo.sqlite3")
        restored = sqlite3.connect(fresh / "nyayo.sqlite3")
        self.assertEqual(restored.execute("SELECT name, secret FROM token ORDER BY rowid").fetchall(),
                         [("hunt", "hash-1"), ("it's", "multi\nline")])
        restored.close()
        self.assertEqual((fresh / "archive" / "matches" / "m1.json").read_text(), '{"match": 1}')
        self.assertEqual((fresh / "games" / "m1" / "1.pgn").read_text(), "1. e4 *")
        self.assertIn("batches", done[-1])

    def test_it_will_not_overwrite_data(self):
        backup.backup(self.data, self.database, repo=self.repo)
        with self.assertRaisesRegex(backup.BackupError, "already has data"):
            backup.restore(self.repo, self.data, self.database)

    def test_it_wants_a_backup(self):
        with self.assertRaisesRegex(backup.BackupError, "not found"):
            backup.restore(self.root, self.root / "fresh", self.root / "fresh" / "x.sqlite3")
