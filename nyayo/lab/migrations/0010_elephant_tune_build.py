"""Elephant exposes its tunable options only in a build made with `make TUNE=1`."""

from django.db import migrations


def set_flags(apps, schema_editor):
    EngineConfig = apps.get_model("lab", "EngineConfig")
    EngineConfig.objects.using(schema_editor.connection.alias).filter(name="Elephant", tune_make_args="").update(tune_make_args="TUNE=1")


def clear_flags(apps, schema_editor):
    EngineConfig = apps.get_model("lab", "EngineConfig")
    EngineConfig.objects.using(schema_editor.connection.alias).filter(name="Elephant", tune_make_args="TUNE=1").update(tune_make_args="")


class Migration(migrations.Migration):

    dependencies = [("lab", "0009_make_args")]
    operations = [migrations.RunPython(set_flags, clear_flags)]
