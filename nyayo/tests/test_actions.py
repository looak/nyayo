"""The frame's login: reading is open, acting needs a signed-in admin user, a bad login is refused."""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

GATED = ["create_test", "create_tune", "create_datagen", "upload_net", "gauntlet", "engines", "books"]
OPEN = ["dashboard", "matches", "runs", "compare", "games", "exports", "machines", "sign_in"]


class Login(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user("owner", password="correct horse", is_staff=True)
        cls.worker = User.objects.create_user("worker", password="battery staple")

    def test_reading_needs_no_login(self):
        for name in OPEN:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_acting_sends_a_signed_out_visitor_to_sign_in_and_back(self):
        for name in GATED:
            url = reverse(name)
            response = self.client.get(url)
            self.assertRedirects(response, f"{reverse('sign_in')}?next={url}", fetch_redirect_response=False)

    def test_signed_in_may_act(self):
        self.client.force_login(self.owner)
        for name in GATED:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_signed_in_without_staff_may_not(self):
        self.client.force_login(self.worker)
        for name in GATED:
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)

    def test_sign_in_goes_back_to_the_page(self):
        response = self.client.post(reverse("sign_in"), {"username": "owner", "password": "correct horse",
                                                         "next": reverse("engines")})
        self.assertRedirects(response, reverse("engines"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("engines")).status_code, 200)

    def test_a_bad_password_is_refused(self):
        response = self.client.post(reverse("sign_in"), {"username": "owner", "password": "wrong"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please enter a correct username and password")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.get(reverse("books")).status_code, 302)

    def test_a_user_who_may_not_act_is_refused_at_sign_in(self):
        response = self.client.post(reverse("sign_in"), {"username": "worker", "password": "battery staple"})
        self.assertContains(response, "can&#x27;t sign in here")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_an_outside_next_is_not_followed(self):
        response = self.client.post(reverse("sign_in"), {"username": "owner", "password": "correct horse",
                                                         "next": "https://example.com/"})
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)

    def test_the_top_bar_says_who_is_signed_in(self):
        self.assertContains(self.client.get(reverse("matches")), "sign in</a>")
        self.client.force_login(self.owner)
        page = self.client.get(reverse("matches"))
        self.assertContains(page, "<b>owner</b>")
        self.assertContains(page, "sign out</button>")

    def test_signing_out_needs_a_post(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("sign_out")).status_code, 405)
        self.assertRedirects(self.client.post(reverse("sign_out")), reverse("dashboard"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("engines")).status_code, 302)

    def test_the_sidebar_has_the_groups_in_order(self):
        page = self.client.get(reverse("matches")).content.decode()
        positions = [page.index(f"<h5>{name}</h5>") for name in ("Main", "Actions", "Lab", "Testing", "Catalogue", "System")]
        self.assertEqual(positions, sorted(positions))
        for name in GATED + ["machines"]:
            self.assertIn(f'href="{reverse(name)}"', page)

    def test_the_api_keeps_its_tokens(self):
        self.assertEqual(self.client.put("/api/matches/x", data="{}", content_type="application/json").status_code, 401)
