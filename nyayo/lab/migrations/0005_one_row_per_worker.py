"""One lab.Machine per worker, as the registration makes it: the rows that were a session each (one for every time a
worker started) are merged into the newest of each box, by the worker's name, or by its user and MAC address when it
had none. A merged row keeps the results of all its sessions: a test two sessions both played has their counters added,
and their steps are kept as far as they do not collide. The rows are `approved` and enabled, as they were working, with
no token: such a worker says hello again and is a new, pending worker, whose name is made unique."""

from django.db import migrations
from django.db.models import BigIntegerField, IntegerField


def clean(value):
    value = value.strip() if isinstance(value, str) else ''
    return '' if value == 'None' else value


def merge(apps, schema_editor):
    Machine = apps.get_model('lab', 'Machine')
    Result = apps.get_model('lab', 'Result')
    Step = apps.get_model('lab', 'Step')
    counters = [f.name for f in Result._meta.get_fields()
                if isinstance(f, (IntegerField, BigIntegerField)) and not f.primary_key and f.name not in ('id',)]

    boxes = {}
    for machine in Machine.objects.order_by('id'):
        info = machine.info if isinstance(machine.info, dict) else {}
        name = clean(info.get('machine_name'))
        key = ('name', name.lower()) if name else ('anonymous', machine.user_id, info.get('mac_address') or '')
        boxes.setdefault(key, []).append(machine)

    taken = set()
    for (kind, *_), sessions in boxes.items():
        keep, older = sessions[-1], sessions[:-1]
        for old in older:
            for result in Result.objects.filter(machine_id=old.id):
                twin = Result.objects.filter(test_id=result.test_id, machine_id=keep.id).first()
                if twin is None:
                    Result.objects.filter(pk=result.pk).update(machine_id=keep.id)
                    continue
                # added in the database, not saved: saving would move `updated`, which dates a finished test
                Result.objects.filter(pk=twin.pk).update(**{name: (getattr(twin, name) or 0) + (getattr(result, name) or 0)
                                                            for name in counters if not name.endswith('_id')})
                for step in Step.objects.filter(result_id=result.id):
                    if Step.objects.filter(result_id=twin.id, step=step.step, side=step.side).exists():
                        step.delete()
                    else:
                        Step.objects.filter(pk=step.pk).update(result_id=twin.id)
                result.delete()
            old.delete()
        info = keep.info if isinstance(keep.info, dict) else {}
        name = clean(info.get('machine_name')) or 'worker-%d' % keep.id
        base, number = name, 1
        while name.lower() in taken:
            number += 1
            name = '%s (%d)' % (base, number)
        taken.add(name.lower())
        Machine.objects.filter(pk=keep.pk).update(name=name[:100], state='approved', enabled=True)


class Migration(migrations.Migration):

    dependencies = [('lab', '0004_worker_registration')]

    operations = [migrations.RunPython(merge, migrations.RunPython.noop)]
