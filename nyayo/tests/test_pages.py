import re

from django.contrib.auth.models import User

from nyayo.runs.models import Run

from .helpers import NyayoTestCase, case, derived, real_run


class Pages(NyayoTestCase):
    def setUp(self):
        super().setUp()
        self.real = self.ingest(real_run())
        child = derived(parents=self.real["run"]["ancestors"], minutes=10)
        case(child, 0, "WAC.001").update(status="fail", got="a2a3")
        self.child = self.ingest(child)

    def get(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        return response.content.decode()

    def test_runs_list(self):
        page = self.get("/")
        self.assertIn("d695c0979c-smoke-20261014T015748", page)
        self.assertIn('class="badge committed"', page)
        self.assertIn("260/300", page)
        self.assertIn("133/325, 1707/3250 pts", page)
        self.assertNotIn(self.real["run"]["id"], self.get("/?state=wip"))
        self.assertIn(self.real["run"]["id"], self.get("/?branch=herd-smoke-report&state=committed"))

    def test_run_page_shows_what_changed(self):
        page = self.get(f"/runs/{self.child['run']['id']}")
        self.assertIn("Newly failing", page)
        self.assertIn("/case/epd.wac_new/WAC.001?branch=herd-smoke-report", page)
        self.assertIn("compared on totals only", page)

    def test_compare_page(self):
        a, b = self.child["run"]["id"], self.real["run"]["id"]
        self.get("/compare")
        self.assertIn("Newly failing", self.get(f"/compare?a={a}&b=baseline"))
        self.assertIn("B is an ancestor of A", self.get(f"/compare?a={a}&b={b}"))
        self.assertIn("Newly passing", self.get(f"/compare?a={b}&b={a}"))
        self.assertIn("Newly failing", self.get(f"/compare?a={a}&b=previous"))
        self.assertEqual(self.client.get("/compare?a=nonsense").status_code, 404)

    def test_case_page(self):
        page = self.get("/case/epd.wac_new/WAC.001?branch=herd-smoke-report")
        self.assertIn("2rr3k/pp3pp1/1nnqbN1p/3pN3/2pP4/2P3Q1/PPB4P/R4RK1 w - - 0 1", page)
        self.assertIn("Failing", page)
        self.assertIn("Smoke_WinAtChess_4Threads", page)
        self.assertIn("<svg", page)
        self.assertIn('title="g3g6">Qg6<', page)
        self.assertIn("Undermine.001", self.get("/case/epd.sts/Undermine.001"))
        self.assertEqual(self.client.get("/case/epd.wac_new/NOPE.999").status_code, 404)

    def test_wrong_method(self):
        self.assertEqual(self.client.get(f"/api/runs/{self.real['run']['id']}").status_code, 405)


class MachineTokens(NyayoTestCase):
    def test_admin_shows_a_working_token_once(self):
        User.objects.create_superuser("owner", "", "pw")
        self.client.login(username="owner", password="pw")
        response = self.client.post("/admin/runs/machine/add/", {"name": "hunt", "active": "on"}, follow=True)
        token = re.search(r"Token for hunt, shown once, copy it now: (\S+)", response.content.decode()).group(1)
        self.client.logout()
        self.assertEqual(self.put(real_run(), token=token).status_code, 201)
        self.assertEqual(Run.objects.get().machine.name, "hunt")
