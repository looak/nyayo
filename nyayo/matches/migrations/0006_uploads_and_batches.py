"""A producer's batch becomes an upload, taken as sent and read by the worker later; nyayo's own 64-game batches are
new. An upload stored before this was kept as bz2 in batches/<match id>/<name>.pgn.bz2 and its games were never put
in batches: it moves to uploads/ as pending, so the worker reads it like any other."""

import os
from pathlib import Path

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


def requeue(apps, schema_editor):
    Upload = apps.get_model("matches", "Upload")
    for upload in Upload.objects.all():
        old = Path(settings.NYAYO_BATCHES_DIR) / upload.match_id / f"{upload.name}.pgn.bz2"
        new = Path(settings.NYAYO_UPLOADS_DIR) / upload.match_id / upload.name
        if old.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old, new)
        upload.state, upload.content_type = "pending", "application/x-bzip2"
        upload.save(update_fields=["state", "content_type"])


class Migration(migrations.Migration):

    dependencies = [
        ("matches", "0005_gamebatch"),
        ("runs", "0002_check_timestamps"),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="gamebatch", name="one_row_per_batch"),
        migrations.RenameModel("GameBatch", "Upload"),
        migrations.RenameField("upload", "batch", "name"),
        migrations.AlterModelOptions(name="upload", options={"ordering": ["match", "name"]}),
        migrations.AlterField(
            model_name="upload", name="match",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="uploads",
                                    to="matches.match")),
        migrations.AlterField(
            model_name="upload", name="machine",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="uploads", to="runs.machine")),
        migrations.AlterField(model_name="upload", name="games", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="upload", name="content_type",
                            field=models.CharField(default="application/x-bzip2", max_length=40)),
        migrations.AddField(model_name="upload", name="encoding", field=models.CharField(blank=True, max_length=20)),
        migrations.AddField(
            model_name="upload", name="state",
            field=models.CharField(choices=[("pending", "pending"), ("done", "done"), ("refused", "refused")],
                                   db_index=True, default="pending", max_length=10)),
        migrations.AddField(model_name="upload", name="reason", field=models.TextField(blank=True)),
        migrations.AddField(model_name="upload", name="appended", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="upload", name="reader", field=models.CharField(blank=True, max_length=40)),
        migrations.AddField(model_name="upload", name="batches", field=models.JSONField(default=list)),
        migrations.AddField(model_name="upload", name="done_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddConstraint(
            model_name="upload",
            constraint=models.UniqueConstraint(fields=("match", "name"), name="one_row_per_upload")),
        migrations.CreateModel(
            name="Batch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("number", models.PositiveIntegerField()),
                ("state", models.CharField(choices=[("open", "open"), ("sealed", "sealed")], db_index=True,
                                           default="open", max_length=10)),
                ("games", models.PositiveIntegerField(default=0)),
                ("bytes", models.PositiveBigIntegerField(default=0)),
                ("stored_bytes", models.PositiveBigIntegerField(default=0)),
                ("sha256", models.CharField(blank=True, max_length=64)),
                ("reader", models.CharField(blank=True, max_length=40)),
                ("sources", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("sealed_at", models.DateTimeField(blank=True, null=True)),
                ("match", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="batches",
                                            to="matches.match")),
            ],
            options={
                "verbose_name_plural": "batches",
                "ordering": ["match", "number"],
                "constraints": [models.UniqueConstraint(fields=("match", "number"),
                                                        name="one_row_per_batch_number")],
            },
        ),
        migrations.RunPython(requeue, migrations.RunPython.noop),
    ]
