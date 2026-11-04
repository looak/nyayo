"""The look of the Machines pages: the status icon and what it says, the response time, the place, the system in sections,
the CPU flags as pills with their meaning, and the system's icon."""

import datetime
import html

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from nyayo.lab import cpuflags, rtt as rtt_module, steps
from nyayo.lab.models import Machine, Place, Step
from nyayo.runs.models import Machine as Token

from .lab_helpers import make_machine, make_result, make_test


def text(client, path="/machines"):
    reply = client.get(path)
    assert reply.status_code == 200, reply.status_code
    return html.unescape(reply.content.decode())


def row(client, name="hunt"):
    return [r for r in client.get("/machines").context["rows"] if r["name"] == name][0]


def age(machine, **delta):
    Machine.objects.filter(pk=machine.pk).update(updated=timezone.now() - datetime.timedelta(**delta))


class Status(TestCase):
    def test_a_healthy_worker_is_fine_idle_or_playing(self):
        machine = make_machine("hunt")
        self.assertEqual(row(self.client)["health"]["look"], "ok")
        machine.workload = make_test().id
        machine.save()
        self.assertEqual((row(self.client)["state"], row(self.client)["health"]["look"]), ("playing", "ok"))

    def test_not_heard_from_for_five_minutes_is_offline(self):
        age(make_machine("hunt"), minutes=6)
        health = row(self.client)["health"]
        self.assertEqual(health["look"], "offline")
        self.assertIn("Not heard from for 5 minutes", health["reasons"][0])

    def test_a_worker_that_plays_no_engine_is_a_warning_that_says_why(self):
        make_machine("hunt", supported=[], toolchains={"make": "4.3"})
        health = row(self.client)["health"]
        self.assertEqual(health["look"], "warn")
        self.assertIn("Plays no engine: Elephant: Missing ['g', 'clang'].", health["reasons"][0])

    def test_a_failed_step_the_worker_is_blamed_for_is_a_warning(self):
        machine = make_machine("hunt")
        result = make_result(make_test(name="lmr"), machine)
        Step.objects.create(result=result, step="compile", side="dev", state="failed", blame="worker", detail="No space left on device")
        health = row(self.client)["health"]
        self.assertEqual(health["look"], "warn")
        self.assertIn("compile dev failed on lmr: No space left on device", health["reasons"][0])

    def test_a_failure_blamed_on_the_engine_is_not_the_workers(self):
        machine = make_machine("hunt")
        result = make_result(make_test(), machine)
        Step.objects.create(result=result, step="bench", side="dev", state="failed", blame="engine")
        self.assertEqual(row(self.client)["health"]["look"], "ok")

    def test_a_failure_on_a_test_that_has_ended_is_over(self):
        machine = make_machine("hunt")
        result = make_result(make_test(finished=True), machine)
        Step.objects.create(result=result, step="compile", side="dev", state="failed", blame="worker")
        self.assertEqual(row(self.client)["health"]["look"], "ok")

    def test_a_held_test_on_it_is_a_warning(self):
        machine = make_machine("hunt")
        test = make_test(name="stuck")
        result = make_result(test, machine)
        Step.objects.create(result=result, step="compile", side="dev", state="failed", blame="unclear", tries=steps.TRIES_UNCLEAR)
        self.assertTrue(steps.held(test))
        health = row(self.client)["health"]
        self.assertEqual(health["look"], "warn")
        self.assertIn("stuck is held after compile dev failed here", health["reasons"][0])

    def test_the_words_and_the_reasons_are_on_the_page_not_only_the_colour(self):
        make_machine("hunt", supported=[], toolchains={"make": "4.3"})
        page = text(self.client)
        self.assertIn('<span class="net-words">Needs a look</span>', page)
        self.assertIn('role="tooltip"', page)
        self.assertIn('aria-label="Needs a look: Plays no engine', page)
        make_machine("tor")
        self.assertIn(">Online<", text(self.client))
        age(Machine.objects.get(name="tor"), minutes=9)
        self.assertIn(">Offline<", text(self.client))

    def test_the_tooltip_is_focusable_for_the_keyboard_and_a_tap(self):
        make_machine("hunt")
        self.assertIn('class="tip net ok" tabindex="0" role="button"', text(self.client))


class Place_(TestCase):
    def staff(self):
        User.objects.create_user("owner", password="pw", is_staff=True, is_superuser=True)
        self.client.login(username="owner", password="pw")

    def test_it_is_shown_on_the_list_and_the_page_and_kept_by_name_across_restarts(self):
        Place.objects.create(name="hunt", text="Stockholm, SE")
        make_machine("Hunt")
        self.assertIn("Stockholm, SE", text(self.client))
        self.assertIn("Stockholm, SE", text(self.client, "/machines/hunt"))
        make_machine("Hunt")  # a restart: another session of the box
        self.assertEqual(row(self.client, "Hunt")["place"], "Stockholm, SE")

    def test_only_someone_signed_in_may_set_it(self):
        make_machine("hunt")
        reply = self.client.post("/machine-place", {"name": "hunt", "text": "Oslo"})
        self.assertEqual(reply.status_code, 302)
        self.assertIn("sign-in", reply["Location"])
        self.assertFalse(Place.objects.exists())
        self.assertNotIn('name="text"', text(self.client, "/machines/hunt"))

    def test_the_form_is_there_when_signed_in_and_saves_and_clears(self):
        make_machine("hunt")
        self.staff()
        self.assertIn('name="text"', text(self.client, "/machines/hunt"))
        reply = self.client.post("/machine-place", {"name": "Hunt", "text": "  Stockholm,   SE  "})
        self.assertRedirects(reply, "/machines/Hunt", fetch_redirect_response=False)
        self.assertEqual(Place.objects.get().text, "Stockholm, SE")
        self.assertEqual(Place.objects.get().name, "hunt")
        self.client.post("/machine-place", {"name": "hunt", "text": ""})
        self.assertFalse(Place.objects.exists())

    def test_it_is_cut_to_what_fits_and_is_text(self):
        make_machine("hunt")
        self.staff()
        self.client.post("/machine-place", {"name": "hunt", "text": "<b>x</b>" * 40})
        self.assertLessEqual(len(Place.objects.get().text), 100)
        self.assertNotIn("<b>x</b>", self.client.get("/machines/hunt").content.decode())

    def test_a_box_with_no_name_or_that_does_not_exist_cannot_have_one(self):
        self.staff()
        unnamed = Machine.objects.create(name="", state="approved", info={})
        self.assertEqual(self.client.post("/machine-place", {"name": "machine %d" % unnamed.id, "text": "x"}).status_code, 404)
        self.assertEqual(self.client.post("/machine-place", {"name": "nobody", "text": "x"}).status_code, 404)
        self.assertIn("has no name", text(self.client, "/machines/machine %d" % unnamed.id))

    def test_a_token_alone_may_have_one(self):
        self.staff()
        token = Token(name="BABYLON")
        token.new_token()
        token.save()
        self.client.post("/machine-place", {"name": "BABYLON", "text": "Home"})
        self.assertIn("Home", text(self.client))


class ResponseTime(TestCase):
    def test_it_is_shown_when_it_was_measured_and_a_dash_when_it_was_not(self):
        make_machine("hunt")
        self.assertEqual(row(self.client)["rtt"], None)
        self.assertIn('data-label="Response" class="num nowrap"><span class="muted">–</span>', text(self.client))
        Machine.objects.update(rtt_ms=12.4)
        self.assertIn(">12 ms<", text(self.client))
        Machine.objects.update(rtt_ms=0.8)
        self.assertIn(">0.8 ms<", text(self.client))
        self.assertIn("0.8 ms", text(self.client, "/machines/hunt"))

    def test_a_box_that_is_offline_shows_no_number(self):
        machine = make_machine("hunt")
        Machine.objects.filter(pk=machine.pk).update(rtt_ms=12.4)
        age(machine, minutes=9)
        self.assertEqual(row(self.client)["rtt"], None)
        self.assertNotIn("12 ms", text(self.client))
        self.assertNotIn("12 ms", text(self.client, "/machines/hunt"))


class Measuring(SimpleTestCase):
    def test_the_round_trip_is_microseconds_at_byte_68_in_ms(self):
        from .test_lab_worker_api import FakeSocket
        self.assertEqual(rtt_module.measure(FakeSocket(12500)), 12.5)
        self.assertEqual(rtt_module.measure(FakeSocket(1)), 0.001)

    def test_anything_wrong_is_none(self):
        from .test_lab_worker_api import FakeSocket
        for sock in (FakeSocket(0), FakeSocket(raises=OSError()), FakeSocket(raises=AttributeError()), FakeSocket(short=True),
                     FakeSocket(4000 * 1000 * 1000), object(), None):
            self.assertIsNone(rtt_module.measure(sock))

    def test_note_reads_the_sockets_in_the_environ_and_names_the_field(self):
        from types import SimpleNamespace
        from .test_lab_worker_api import FakeSocket
        machine = Machine()
        rtt_module.note(SimpleNamespace(META={"gunicorn.socket": FakeSocket(2000)}), machine)
        self.assertEqual((machine.rtt_ms, rtt_module.changed(machine), rtt_module.values(machine)), (2.0, ["rtt_ms"], {"rtt_ms": 2.0}))
        rtt_module.note(SimpleNamespace(META={}), machine)  # runserver has none
        self.assertEqual(rtt_module.changed(machine), [])
        self.assertEqual(rtt_module.changed(Machine()), [])


class SystemInSections(TestCase):
    def test_the_boxes_of_the_page(self):
        make_machine("hunt", cpu_name="Ryzen 9", isa_name="x86-64-avx2", logical_cores=24, physical_cores=12, ram_total_mb=30914,
                     huge_pages="madvise", python_ver="3.12.3", fastchess_ver="1.8.2", os_ver="6.8", cxx_comp="g++",
                     toolchains={"clang": "18.1.3", "make": "4.3", "python3": "3.12.3"})
        page = text(self.client, "/machines/hunt")
        for heading in ("Hardware", "Toolchains", "Software", "Engines"):
            self.assertIn(">%s</h2>" % heading, page)
        for expected in ("Ryzen 9", "12 cores, 24 threads", "x86-64-avx2", "30.2 GB (30,914 MB)", "madvise", "3.12.3", "1.8.2",
                         "Linux 6.8", "Elephant", "clang 18.1.3", "C++ for fastchess"):
            self.assertIn(expected, page)
        self.assertIn("none reported", page)

    def test_gpus_as_names_or_objects(self):
        make_machine("hunt", gpus=["NVIDIA RTX 4090", {"name": "Radeon", "vram": "16 GB", "token": "NOT-SHOWN"}, 7, {"x": 1}])
        page = text(self.client, "/machines/hunt")
        self.assertIn("NVIDIA RTX 4090", page)
        self.assertIn("Radeon", page)
        self.assertIn("16 GB", page)
        self.assertNotIn("NOT-SHOWN", page)
        self.assertNotIn("GPU</dt><dd>none reported", page)


class FlagPills(SimpleTestCase):
    def test_the_names_match_however_they_are_written(self):
        for written in ("SSE4.1", "sse4_1", "SSE41", "sse4-1"):
            self.assertEqual(cpuflags.describe(written), cpuflags.HELP["SSE41"])
        self.assertEqual(cpuflags.describe("MADEUP"), "")

    def test_the_table_has_the_common_ones_and_says_what_they_are(self):
        self.assertIn("256-bit", cpuflags.describe("AVX2"))
        for flag in ("POPCNT", "BMI2", "AVX512F", "AVX512VNNI", "SSSE3", "FMA", "AVX512BW"):
            self.assertTrue(cpuflags.describe(flag), flag)


class FlagPillsOnThePage(TestCase):
    def test_a_known_flag_has_its_meaning_in_a_tooltip_and_an_unknown_one_is_plain(self):
        make_machine("hunt", cpu_flags=["AVX2", "MADEUP", "POPCNT"])
        page = text(self.client, "/machines/hunt")
        self.assertIn('<span class="tip flagpill known" tabindex="0" role="button" aria-label="AVX2: 256-bit integer and float vector instructions', page)
        self.assertIn('<b>AVX2</b>: 256-bit integer and float vector instructions; what NNUE evaluation runs on', page)
        self.assertIn('<span class="flagpill">MADEUP</span>', page)
        self.assertEqual(page.count('class="tip flagpill known"'), 2)


class SystemIcon(TestCase):
    def test_linux_windows_and_macos_have_one_each_at_one_size(self):
        for name, os_name, label in (("a", "Linux", "Linux"), ("b", "Windows", "Windows"), ("c", "Darwin", "macOS")):
            make_machine(name, os_name=os_name, supported=[])
        page = text(self.client)
        for label in ("Linux", "Windows", "macOS"):
            self.assertIn('aria-label="%s"' % label, page)
        self.assertEqual({r["os_kind"] for r in self.client.get("/machines").context["rows"]}, {"linux", "windows", "macos"})

    def test_wsl_is_linux_with_a_tag_when_the_kernel_says_so(self):
        make_machine("pc", os_ver="6.18.54.1-microsoft-standard-WSL2")
        make_machine("plain", os_ver="6.8.0-generic")
        self.assertEqual((row(self.client, "pc")["os_kind"], row(self.client, "pc")["wsl"]), ("linux", True))
        self.assertFalse(row(self.client, "plain")["wsl"])
        self.assertEqual(text(self.client).count('<span class="wsl"'), 1)

    def test_an_unknown_system_still_has_an_icon(self):
        make_machine("odd", os_name="Plan9", supported=[])
        self.assertIn('aria-label="unknown system"', text(self.client))


class NoSecretsInTheNewFields(TestCase):
    def test_nothing_credential_like_leaks_through_the_gpus_the_place_or_the_flags(self):
        make_machine("hunt", gpus=[{"name": "GPU", "token": "THE-GPU-TOKEN", "password": "THE-GPU-PASSWORD"}], api_key="THE-API-KEY",
                     cpu_flags=["AVX2"], tokens={"Elephant": "ghp_THE_GIT_TOKEN"}, mac_address="de:ad:be:ef", secret="THE-INFO-SECRET",
                     cxx_comp="g++", rtt_ms=5)
        Machine.objects.update(secret="THE-SESSION-SECRET", rtt_ms=5.0)
        Place.objects.create(name="hunt", text="Stockholm, SE")
        for path in ("/machines", "/machines/hunt"):
            page = text(self.client, path)
            for secret in ("THE-GPU-TOKEN", "THE-GPU-PASSWORD", "THE-API-KEY", "ghp_THE_GIT_TOKEN", "de:ad:be:ef", "THE-INFO-SECRET",
                           "THE-SESSION-SECRET"):
                self.assertNotIn(secret, page, path)
