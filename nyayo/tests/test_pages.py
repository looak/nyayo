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
        child["checks"][0]["metrics"]["solved"]["value"] = 259
        self.child = self.ingest(child)

    def get(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        return response.content.decode()

    def test_runs_list(self):
        page = self.get("/")
        self.assertLess(page.index(self.child["run"]["id"]), page.index(self.real["run"]["id"]))
        self.assertIn('herd-smoke-report <span class="mono">d695c09</span></a> <span class="muted">smoke</span>', page)
        self.assertIn('class="badge committed"', page)
        self.assertIn('<span class="outcome pass">passed</span>', page)
        self.assertIn('657<span class="muted">/925</span>', page)
        self.assertIn('title="Smoke_STS: passed, 133/325, 1707/3250 pts, 12.8 s"', page)
        self.assertNotIn(self.real["run"]["id"], self.get("/?kind=extended"))
        self.assertIn(self.real["run"]["id"], self.get("/?kind=smoke"))
        self.assertIn('<td class="num nowrap">41.0 s</td>', page)
        self.assertNotIn(self.real["run"]["id"], self.get("/?state=wip"))
        self.assertIn(self.real["run"]["id"], self.get("/?branch=herd-smoke-report&state=committed"))

    def test_run_page_shows_what_changed(self):
        page = self.get(f"/runs/{self.child['run']['id']}")
        self.assertIn("Newly failing", page)
        self.assertIn("/case/epd.wac_new/WAC.001?branch=herd-smoke-report", page)
        self.assertIn('<span class="outcome pass">passed</span>', page)
        self.assertIn("<code>nodes 150000</code><code>1 thread</code><code>64 MB</code>", page)
        self.assertIn('<a class="cell newly-failing" href="/case/epd.wac_new/WAC.001?branch=herd-smoke-report" '
                      'title="WAC.001: fail, 52 ms"', page)
        self.assertIn("never compared\">13.7 s</span>", page)
        self.assertIn("2026-10-14 01:57:07 &ndash; 01:57:48 <span class=\"muted\">41.0 s</span>", page)
        self.assertIn('<span class="mono">STS1.epd</span> <span class="muted">', page)
        self.assertIn('<span class="score-value">259<span class="score-total">/300</span></span>', page)
        self.assertIn('<span class="delta worse">−1</span>', page)
        self.assertIn('title="1 failed, passed before"', page)
        self.assertIn('title="minimum 252"', page)
        self.assertIn('title="baseline run 260"', page)
        self.assertIn('title="1707 points earned"', page)
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
        self.assertIn('<td class="num nowrap">52 ms</td>', page)
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
