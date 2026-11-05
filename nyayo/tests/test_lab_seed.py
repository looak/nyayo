"""What the data migration seeds: Elephant's engine with its corrected time controls, and the lab's twelve books."""

import re

from django.test import TestCase

from nyayo.lab.models import Book, EngineConfig, ServerState


def parse_time_control(time_str):
    """The fischer and cyclic part of OpenBench's TimeControl.parse (OpenBench/utils.py, openbench_eg da629d7), which
    is not copied into nyayo; this is what a worker is told to play at."""
    pattern = r'(?P<moves>(\d+/)?)(?P<base>\d*(\.\d+)?)(?P<inc>\+(\d+\.)?\d+)?'
    moves, base, inc = re.search(pattern, time_str).group('moves', 'base', 'inc')
    moves = None if moves == '' else moves.rstrip('/')
    inc = 0.0 if inc is None else inc.lstrip('+')
    if moves is None:
        return '%.1f+%.2f' % (float(base), float(inc))
    return '%d/%.1f+%.2f' % (int(moves), float(base), float(inc))


class TimeControls(TestCase):
    def test_what_the_file_wrote_loses_its_increment(self):
        # why the seed writes a +: these are the strings in Engines/elephant.json
        self.assertEqual(parse_time_control("8.00.08"), "8.0+0.00")
        self.assertEqual(parse_time_control("40.00.4"), "40.0+0.00")

    def test_the_seeded_ones_keep_theirs(self):
        presets = EngineConfig.objects.get(name="Elephant").presets["test_presets"]
        played = {name: parse_time_control(presets[name]["both_time_control"])
                  for name in ("STC", "LTC", "SMP STC", "SMP LTC")}
        self.assertEqual(played, {"STC": "8.0+0.08", "LTC": "40.0+0.40", "SMP STC": "5.0+0.05",
                                  "SMP LTC": "20.0+0.20"})


class Seeds(TestCase):
    def test_elephant_is_there_with_its_build_and_presets(self):
        elephant = EngineConfig.objects.get(name="Elephant")
        self.assertEqual(elephant.source, "https://github.com/looak/elephant")
        self.assertEqual(elephant.nps, 1487000)
        self.assertFalse(elephant.private)
        self.assertEqual(elephant.build(), {"path": "ob_build", "compilers": ["g", "clang"], "cpuflags": ["POPCNT"],
                                            "systems": ["Linux", "Windows"]})
        self.assertEqual(set(elephant.presets), {"test_presets", "tune_presets", "datagen_presets"})
        stc = elephant.presets["test_presets"]["STC"]
        self.assertEqual((stc["both_options"], stc["workload_size"]), ("Threads=1 Hash=8", 32))
        self.assertEqual(elephant.presets["test_presets"]["default"]["book_name"], "8moves_v3.epd")

    def test_the_twelve_books_are_there(self):
        self.assertEqual(Book.objects.count(), 12)
        book = Book.objects.get(name="8moves_v3.epd")
        self.assertEqual(book.sha, "1f055af431656f09ee6a09d2448e0b876125f78bb7b404fca2031c403a1541e5")
        self.assertEqual(book.source, "https://raw.githubusercontent.com/AndyGrant/openbench-books/master/8moves_v3.epd.zip")
        self.assertTrue(all(b.enabled and re.fullmatch(r"[0-9a-f]{64}", b.sha) for b in Book.objects.all()))

    def test_the_build_checksum_is_set_and_follows_the_build(self):
        seeded = ServerState.checksum()
        self.assertEqual(len(seeded), 64)
        elephant = EngineConfig.objects.get(name="Elephant")
        elephant.nps = 1
        elephant.save()
        self.assertEqual(ServerState.checksum(), seeded)
        elephant.build_cpuflags = "POPCNT BMI2"
        elephant.save()
        self.assertNotEqual(ServerState.checksum(), seeded)
