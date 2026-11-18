"""`manage.py jobs`, and `manage.py worker`, its old name, which runs the same code with a notice."""
import io

from django.core.management import call_command
from django.test import TestCase


class JobsCommand(TestCase):
    def test_once_drains_the_queue_and_exits(self):
        out = io.StringIO()
        call_command("jobs", "--once", stdout=out)
        self.assertIn("things done", out.getvalue())

    def test_the_old_name_still_runs_it_and_says_so_once(self):
        out, err = io.StringIO(), io.StringIO()
        import contextlib
        with contextlib.redirect_stderr(err):
            call_command("worker", "--once", stdout=out)
        self.assertIn("things done", out.getvalue())
        self.assertEqual(err.getvalue().count("manage.py jobs"), 1)
