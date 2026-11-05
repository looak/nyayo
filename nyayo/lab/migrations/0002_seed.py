"""Seeds: Elephant's EngineConfig (from openbench_eg's Engines/elephant.json) and the lab's twelve books (its
Books/*.json), and the id the first test takes (ids.py).

Elephant's time controls are written with a `+` here. The file has `8.00.08`, `40.00.4`, `5.00.05` and `20.00.2`, which
OpenBench's TimeControl.parse reads as 8.00, 40.00, 5.00 and 20.00 with no increment; these are the intended
8.0+0.08, 40.0+0.4, 5.0+0.05 and 20.0+0.2 (plans/nyayo-2.md, open decision 5).
"""

import hashlib
import json

from django.db import migrations

from nyayo.lab.ids import first_test_id, start_test_ids

BOOK_SOURCE = "https://raw.githubusercontent.com/AndyGrant/openbench-books/master/%s.zip"

BOOKS = [
    ("2moves_v1.epd", "7bec98239836f219dc41944a768c0506abed950aaec48da69a0782643e90f237"),
    ("3moves_FRC.epd", "6bf81e1ada6a3306bbc8356f7bca1e2984a2828d658799992d5443b7179c934d"),
    ("4moves_noob.epd", "4be746a91e3f8af0c9344b1e72d611e9fcfe486843867a55760970a4896f284d"),
    ("8moves_v3.epd", "1f055af431656f09ee6a09d2448e0b876125f78bb7b404fca2031c403a1541e5"),
    ("DFRC.epd", "648c447ef40614a44d13b78911e81470d8ddb0d3b2711c1b180e990871f5db4f"),
    ("Endgames.epd", "71c7477ca8c8fb097bb565794d8b5ed8754532f8fb499e9d8b1d397692302a6a"),
    ("Pohl.epd", "b3e64e0dab84cf451a9ac7ef031f5a2bbcf16c7e21be95298fb03cbf021f5466"),
    ("Pohl.pgn", "aaa7559c5d94e592fe5cca3586cb099d8fc5f13428d4ce84afc4b97811241c7e"),
    ("UHO_4060_v2.epd", "36f2ec751ab78def6be1307430cbe2cd2ba65ade8d2aaae8f10e3df7d0ea83e1"),
    ("UHO_4060_vB.epd", "68c58a5f6a5e068a30376efa19a6bcd78f8ec8bb67ad984dd8d156abfa81de96"),
    ("UHO_4060_vT.epd", "f5da53b91b85f5ddc72c352522a8056ab8bbf0058b67d799c4ca96effd5d5f26"),
    ("UHO_Lichess_4852_v1.epd", "7a7f6470615a69c6cf23d565417701d38732876f480af90d67b42abade35644a"),
]

ELEPHANT = {
    "private": False,
    "nps": 1487000,
    "source": "https://github.com/looak/elephant",
    "build_path": "ob_build",
    "build_compilers": "g clang",
    "build_cpuflags": "POPCNT",
    "build_systems": "Linux Windows",
    "presets": {
        "test_presets": {
            "default": {
                "base_branch": "main",
                "book_name": "8moves_v3.epd",
                "test_bounds": "[0.00, 5.00]",
                "test_confidence": "[0.05, 0.05]",
                "win_adj": "movecount=3 score=400",
                "draw_adj": "movenumber=40 movecount=8 score=10",
            },
            "STC": {"both_options": "Threads=1 Hash=8", "both_time_control": "8.0+0.08", "workload_size": 32},
            "LTC": {"both_options": "Threads=1 Hash=64", "both_time_control": "40.0+0.4", "workload_size": 8},
            "SMP STC": {"both_options": "Threads=8 Hash=64", "both_time_control": "5.0+0.05", "workload_size": 64},
            "SMP LTC": {"both_options": "Threads=8 Hash=256", "both_time_control": "20.0+0.2", "workload_size": 16},
        },
        "tune_presets": {"default": {}},
        "datagen_presets": {"default": {}},
    },
}


def seed(apps, schema_editor):
    Book = apps.get_model("lab", "Book")
    EngineConfig = apps.get_model("lab", "EngineConfig")
    ServerState = apps.get_model("lab", "ServerState")
    Match = apps.get_model("matches", "Match")
    using = schema_editor.connection.alias

    for name, sha in BOOKS:
        Book.objects.using(using).get_or_create(name=name, defaults={"source": BOOK_SOURCE % name, "sha": sha})

    config, _ = EngineConfig.objects.using(using).get_or_create(name="Elephant", defaults=ELEPHANT)

    # EngineConfig.save() refreshes the build checksum, but a migration's historical model has none of that
    checksum = hashlib.sha256(b"").digest()
    for each in EngineConfig.objects.using(using).all():
        build = {
            "path": each.build_path,
            "compilers": each.build_compilers.split(),
            "cpuflags": each.build_cpuflags.split(),
            "systems": each.build_systems.split(),
        }
        partial = hashlib.sha256(json.dumps([each.name, build], sort_keys=True).encode("utf-8")).digest()
        checksum = bytes(a ^ b for a, b in zip(checksum, partial))
    ServerState.objects.using(using).update_or_create(pk=1, defaults={"build_checksum": checksum.hex()})

    match_ids = Match.objects.using(using).values_list("id", flat=True)
    start_test_ids(first_test_id(match_ids), schema_editor.connection)


class Migration(migrations.Migration):

    dependencies = [
        ("lab", "0001_models"),
        ("matches", "0010_states_and_verdict"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
