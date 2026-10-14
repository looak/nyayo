import json
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.utils import timezone

from nyayo.runs.models import Case, Check, Machine, Metric, Run

from .helpers import NyayoTestCase, body, case, derived, iso, real_run


class CreateAndUpdate(NyayoTestCase):
    def test_create_indexes_the_whole_run(self):
        document = real_run()
        response = self.put(document)
        self.assertEqual(response.status_code, 201)
        run = Run.objects.get()
        self.assertEqual(str(run.pk), document["run"]["id"])
        self.assertEqual(run.machine, self.machine)
        self.assertEqual(run.state, "committed")
        self.assertEqual(len(run.ancestors), 300)
        self.assertEqual(Check.objects.count(), 3)
        self.assertEqual(Case.objects.count(), 925)
        sts = Check.objects.get(name="Smoke_STS")
        self.assertEqual((sts.metric("points"), sts.metric("max_points")), (1707, 3250))
        self.assertEqual(sts.per_file, 25)
        undermine = Case.objects.get(check_result=sts, case_id="Undermine.001")
        self.assertEqual((undermine.points, undermine.max_points, undermine.got), (10, 10, "f4f5"))
        self.assertEqual(undermine.file_sha256, sts.params["file_sha256"]["STS1.epd"])
        self.assertTrue(Metric.objects.filter(name="duration", unit="s").exists())

    def test_archive_holds_the_raw_bytes(self):
        document = real_run()
        raw = body(document)
        self.put(document, raw=raw)
        self.assertEqual((self.archive / f"{document['run']['id']}.json").read_bytes(), raw)
        meta = json.loads((self.archive / f"{document['run']['id']}.meta.json").read_text())
        self.assertEqual(meta["machine"], "babylon")

    def test_gzip_body(self):
        response = self.put(real_run(), gzipped=True)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Case.objects.count(), 925)

    def test_same_run_again_is_an_update(self):
        document = real_run()
        self.assertEqual(self.put(document).status_code, 201)
        response = self.put(document)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"updated\n")
        self.assertEqual(Run.objects.count(), 1)
        self.assertEqual(Case.objects.count(), 925)

    def test_publish(self):
        document = self.ingest(real_run())
        document["run"]["state"] = "published"
        document["run"]["published_at"] = iso(timezone.now())
        self.assertEqual(self.put(document).status_code, 200)
        run = Run.objects.get()
        self.assertEqual(run.state, "published")
        self.assertIsNotNone(run.published_at)
        self.assertEqual(json.loads((self.archive / f"{run.pk}.json").read_bytes())["run"]["state"], "published")

    def test_wip_is_committed_then_published(self):
        document = derived(state="wip")
        self.assertEqual(self.put(document).status_code, 201)
        parents = document["run"]["ancestors"]
        commit = "ab" * 20
        document["run"].update(state="committed", commit=commit, committed_at=iso(timezone.now()),
                               ancestors=[commit] + parents)
        self.assertEqual(self.put(document).status_code, 200)
        document["run"].update(state="published", published_at=iso(timezone.now()))
        self.assertEqual(self.put(document).status_code, 200)
        run = Run.objects.get()
        self.assertEqual((run.state, run.commit, run.ancestors[0]), ("published", commit, commit))


class StateRules(NyayoTestCase):
    def published(self):
        document = self.ingest(real_run())
        document["run"]["state"] = "published"
        self.ingest(document)
        return document

    def test_state_never_goes_back(self):
        document = self.published()
        document["run"]["state"] = "committed"
        response = self.put(document)
        self.assertEqual(response.status_code, 409)
        self.assertIn(b"published to committed", response.content)
        self.assertEqual(Run.objects.get().state, "published")
        self.assertEqual(json.loads((self.archive / f"{document['run']['id']}.json").read_bytes())["run"]["state"],
                         "published")

    def test_commit_changes_once(self):
        document = self.ingest(real_run())
        document["run"]["commit"] = "cd" * 20
        response = self.put(document)
        self.assertEqual(response.status_code, 409)
        self.assertIn(b"run.commit is already", response.content)

    def test_fixed_run_fields_name_the_field(self):
        document = self.ingest(real_run())
        document["run"]["tree"] = "ef" * 20
        response = self.put(document)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.content.startswith(b"run.tree changed"), response.content)

    def test_results_are_fixed(self):
        document = self.ingest(real_run())
        case(document, 0, "WAC.001")["got"] = "a2a3"
        response = self.put(document)
        self.assertEqual(response.status_code, 409)
        self.assertIn(b"checks[0].cases[0].got changed", response.content)
        self.assertEqual(Case.objects.get(check_result__ordinal=0, case_id="WAC.001").got, "g3g6")

    def test_branch_is_fixed(self):
        document = self.ingest(real_run())
        document["run"]["branch"] = "main"
        self.assertEqual(self.put(document).status_code, 409)


class Refused(NyayoTestCase):
    def test_tokens(self):
        document = real_run()
        self.assertEqual(self.put(document, token="nonsense").status_code, 401)
        response = self.client.put(f"/api/runs/{document['run']['id']}", data=body(document),
                                   content_type="application/json")
        self.assertEqual(response.status_code, 401)
        Machine.objects.update(active=False)
        self.assertEqual(self.put(document).status_code, 401)
        self.assertFalse(Run.objects.exists())

    def assertRefused(self, document=None, reason=b"", **put):
        response = self.put(document if document is not None else real_run(), **put)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn(reason, response.content)
        self.assertFalse(Run.objects.exists())
        self.assertFalse(any(self.archive.iterdir()))

    def test_schema_version(self):
        document = real_run()
        document["schema_version"] = 2
        self.assertRefused(document, b"schema_version must be 1")

    def test_not_json(self):
        document = real_run()
        self.assertRefused(document, b"not JSON", raw=b"{nope")

    def test_url_and_body_ids_differ(self):
        self.assertRefused(reason=b"doesn't match the url", run_id="0b9a8f1e-0000-4000-8000-000000000000")

    def test_wip_with_a_commit(self):
        document = derived(state="wip")
        document["run"]["commit"] = "ab" * 20
        self.assertRefused(document, b"a wip run has no commit")

    def test_committed_without_a_commit(self):
        document = real_run()
        document["run"]["commit"] = None
        self.assertRefused(document, b"needs run.commit")

    def test_two_checks_with_one_key(self):
        document = real_run()
        document["checks"].append(document["checks"][0])
        self.assertRefused(document, b"searched exactly like checks[0]")

    def test_bad_case_status(self):
        document = real_run()
        document["checks"][0]["cases"][3]["status"] = "maybe"
        self.assertRefused(document, b"checks[0].cases[3].status")


class Expiry(NyayoTestCase):
    def test_expire_runs(self):
        long_ago = timezone.now() - timedelta(days=60)
        old_wip = derived(state="wip")
        old_wip["run"]["recorded_at"] = iso(long_ago)
        new_wip = derived(state="wip")
        new_wip["run"]["recorded_at"] = iso(timezone.now())
        orphan = derived()
        orphan["run"]["committed_at"] = iso(long_ago)
        old_published = derived(state="published")
        old_published["run"]["recorded_at"] = iso(long_ago)
        recent_commit = derived()
        recent_commit["run"]["committed_at"] = iso(timezone.now() - timedelta(days=3))
        for document in (old_wip, new_wip, orphan, old_published, recent_commit):
            self.ingest(document)

        call_command("expire_runs", stdout=StringIO())
        kept = {str(pk) for pk in Run.objects.values_list("pk", flat=True)}
        self.assertEqual(kept, {new_wip["run"]["id"], old_published["run"]["id"], recent_commit["run"]["id"]})
        self.assertFalse((self.archive / f"{old_wip['run']['id']}.json").exists())
        self.assertTrue((self.archive / f"{new_wip['run']['id']}.json").exists())
        self.assertEqual(Case.objects.count(), 3 * 925)

    def test_committed_without_committed_at_ages_from_recorded_at(self):
        # the fixture was promoted before committed_at existed and was recorded in October 2026
        self.ingest(real_run())
        with self.settings(NYAYO_COMMITTED_DAYS=100000):
            call_command("expire_runs", stdout=StringIO())
        self.assertEqual(Run.objects.count(), 1)

    def test_dry_run(self):
        document = derived(state="wip")
        document["run"]["recorded_at"] = iso(timezone.now() - timedelta(days=60))
        self.ingest(document)
        out = StringIO()
        call_command("expire_runs", "--dry-run", stdout=out)
        self.assertIn("1 run(s) would expire", out.getvalue())
        self.assertEqual(Run.objects.count(), 1)
