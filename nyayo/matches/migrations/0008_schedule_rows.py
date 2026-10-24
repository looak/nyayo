"""A gauntlet's schedule becomes a row per game, with the seq and digest of reports by changes on the match. The JSON
value in the Match row is copied into rows (with each entry's hash and the schedule's digest) and dropped."""

import hashlib
import json

import django.db.models.deletion
from django.db import migrations, models

FIELDS = ("number", "round", "opponent", "subject_colour", "state", "result", "termination", "fault")


def entry_hash(entry):
    text = json.dumps({name: entry.get(name) for name in FIELDS}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def to_rows(apps, schema_editor):
    Match = apps.get_model("matches", "Match")
    ScheduleEntry = apps.get_model("matches", "ScheduleEntry")
    for match in Match.objects.exclude(schedule=[]):
        rows, digest = [], 0
        for entry in match.schedule:
            hashed = entry_hash(entry)
            digest ^= int(hashed, 16)
            rows.append(ScheduleEntry(
                match=match, number=entry["number"], round=entry.get("round"), opponent=entry["opponent"],
                subject_colour=entry["subject_colour"], state=entry["state"], result=entry.get("result"),
                termination=entry.get("termination"), fault=entry.get("fault"), hash=hashed))
        ScheduleEntry.objects.bulk_create(rows, batch_size=500)
        match.schedule_digest = f"{digest:064x}"
        match.save(update_fields=["schedule_digest"])


def to_json(apps, schema_editor):
    Match = apps.get_model("matches", "Match")
    ScheduleEntry = apps.get_model("matches", "ScheduleEntry")
    for match in Match.objects.all():
        match.schedule = [{name: getattr(row, name) for name in FIELDS}
                          for row in ScheduleEntry.objects.filter(match=match).order_by("number")]
        match.save(update_fields=["schedule"])


class Migration(migrations.Migration):

    dependencies = [
        ("matches", "0007_kept_single_games"),
    ]

    operations = [
        migrations.AddField(model_name="match", name="report_seq",
                            field=models.PositiveBigIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="match", name="schedule_digest",
                            field=models.CharField(blank=True, max_length=64)),
        migrations.CreateModel(
            name="ScheduleEntry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("number", models.PositiveIntegerField()),
                ("round", models.IntegerField(blank=True, null=True)),
                ("opponent", models.CharField(max_length=64)),
                ("subject_colour", models.CharField(max_length=5)),
                ("state", models.CharField(max_length=10)),
                ("result", models.CharField(blank=True, max_length=10, null=True)),
                ("termination", models.CharField(blank=True, max_length=200, null=True)),
                ("fault", models.JSONField(blank=True, null=True)),
                ("hash", models.CharField(max_length=64)),
                ("match", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="scheduled",
                                            to="matches.match")),
            ],
            options={
                "verbose_name_plural": "schedule entries",
                "ordering": ["match", "number"],
                "constraints": [models.UniqueConstraint(fields=("match", "number"),
                                                        name="one_entry_per_game_number")],
            },
        ),
        migrations.RunPython(to_rows, to_json),
        migrations.RemoveField(model_name="match", name="schedule"),
    ]
