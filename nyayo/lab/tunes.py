"""Tunes (SPSA) beyond what OpenBench had: the momentum update, snapshots of the values, the outcome checks that play
a snapshot against the start, the averaged result and the SPRT that can follow. The design is in ramani's
plans/nyayo-2.md, Tunes.

advance() is called from utils.update_test, inside the transaction that has the tune's row locked, with the delta the
worker reported. Everything it writes is therefore consistent with the values it moved, and two reports never
interleave. An "iteration" here is a completed one: games // (2 * pairs_per), so the start is iteration 0 and the end is
the run's `iterations`.
"""

import csv
import io
import math

from django.db import transaction

from .models import SPSAParameter, SPSASnapshot, Test
from .stats import Elo


def number(value, is_float=True):

    # A value as it is written in an option: integers whole, floats without trailing zeros
    if not is_float:
        return str(int(round(value)))
    text = ('%.6f' % value).rstrip('0').rstrip('.')
    return '0' if text in ('', '-0') else text


def append_options(options, pairs):

    # The way a preset's options are carried: Name=value, space separated, after what is there
    return ' '.join([options.strip()] + ['%s=%s' % (name, text) for name, text in pairs]).strip()


def current_values(run):
    return { p.name : p.value for p in run.parameters.order_by('index') }


def start_values(run):
    return { p.name : p.start for p in run.parameters.order_by('index') }


def as_options(run, values):

    # A set of values as the pairs to append: integer parameters rounded, in the parameters' order
    return [(p.name, number(values[p.name], p.is_float)) for p in run.parameters.order_by('index') if p.name in values]


def frozen_options(run):
    return [(f['name'], number(float(f['value']))) for f in run.frozen or []]


def iteration_of(run, games):
    return min(run.iterations, games // (2 * run.pairs_per))


def default_snapshot_every(iterations):
    return max(1, iterations // 100)


def default_check_every(iterations):
    return iterations // 20


def update_values(run, parameters, deltas):

    # The rule, applied to the values in place. PLAIN is OpenBench's: add the delta. MOMENTUM keeps a share of the step
    # before: velocity = m * velocity + (1 - m) * delta, which for a steady delta settles on that delta, so a step is the
    # same size as plain SPSA's, but noise cancels and a parameter pushed one way keeps moving.
    momentum = run.momentum if run.update_rule == run.UpdateRule.MOMENTUM else None

    for delta, param in zip(deltas, parameters):
        if momentum is not None:
            param.velocity = momentum * param.velocity + (1.0 - momentum) * delta
            delta = param.velocity
        param.value = max(param.min_value, min(param.max_value, param.value + delta))


def advance(test, deltas, games_before):

    # Moves the values, and writes the snapshot, check and follow-up test the new games make due. Returns nothing; sets
    # test.finished. `test.games` already holds the new games.
    run        = test.spsa_run
    parameters = list(run.parameters.order_by('index'))

    update_values(run, parameters, deltas)
    SPSAParameter.objects.bulk_update(parameters, ['value', 'velocity'])

    test.finished = test.games >= 2 * run.pairs_per * run.iterations

    step   = 2 * run.pairs_per
    before = games_before // step
    after  = test.games // step
    values = { p.name : p.value for p in parameters }

    snapshot_due = run.snapshot_every > 0 and after // run.snapshot_every > before // run.snapshot_every
    check_due    = run.check_every > 0 and after // run.check_every > before // run.check_every

    if snapshot_due or check_due or test.finished:
        snapshot = snap(run, iteration_of(run, test.games), test.games, values)
        if check_due and snapshot.check_test_id is None:
            make_check(test, run, snapshot)

    if test.finished and run.then_sprt and run.then_test_id is None:
        run.then_test = queue_sprt(test)
        run.save(update_fields=['then_test'])


def snap(run, iteration, games, values):

    # One snapshot an iteration: a second write for the same iteration only brings the values up to date
    snapshot, created = SPSASnapshot.objects.get_or_create(spsa_run=run, iteration=iteration,
                                                           defaults={ 'games' : games, 'values' : values })
    if not created:
        snapshot.games, snapshot.values = games, values
        snapshot.save(update_fields=['games', 'values'])
    return snapshot


def start_snapshot(run):

    # At creation: iteration 0, the start values
    return snap(run, 0, 0, start_values(run))


def queue_test(tune, name, dev_values, base_values, **fields):

    # A test of the tune's engine at its commit and time control, the tune's options and the frozen values on both
    # sides, dev playing `dev_values` and base `base_values`. Returns the Test, reported once committed.
    from . import modify_test

    run  = tune.spsa_run
    test = Test(
        author=fields.pop('author', None) or tune.author, name=name[:200], info=fields.pop('info', ''),
        upload_pgns=tune.upload_pgns, runner=tune.runner, book_name=tune.book_name,
        dev=tune.dev, dev_repo=tune.dev_repo, dev_engine=tune.dev_engine, dev_network=tune.dev_network,
        dev_netname=tune.dev_netname, dev_time_control=tune.dev_time_control,
        dev_options=append_options(tune.dev_options, as_options(run, dev_values)),
        base=tune.base, base_repo=tune.base_repo, base_engine=tune.base_engine, base_network=tune.base_network,
        base_netname=tune.base_netname, base_time_control=tune.base_time_control,
        base_options=append_options(tune.base_options, as_options(run, base_values)),
        workload_size=tune.workload_size, throughput=tune.throughput, priority=tune.priority + fields.pop('raise_by', 0),
        syzygy_wdl=tune.syzygy_wdl, syzygy_adj=tune.syzygy_adj, win_adj=tune.win_adj, draw_adj=tune.draw_adj,
        scale_method=tune.scale_method, scale_nps=tune.scale_nps, approved=True, **fields,
    )
    return test, lambda: transaction.on_commit(lambda: modify_test.report(test))


def make_check(tune, run, snapshot):

    # A games test of the snapshot's values against the start: a higher priority, so a check is played before more of the tune
    name = '%s check @%d' % (tune.name or 'Tune %d' % tune.id, snapshot.iteration)
    test, report = queue_test(tune, name, snapshot.values, start_values(run), test_mode='GAMES', max_games=run.check_games,
                              raise_by=1, info='Outcome check of tune %d at iteration %d' % (tune.id, snapshot.iteration))
    test.save()
    snapshot.check_test = test
    snapshot.save(update_fields=['check_test'])
    report()
    return test


def check_finished(test):

    # A games test that has finished: if it was a check, its Elo and error go on its snapshot
    snapshots = SPSASnapshot.objects.filter(check_test=test)
    if not snapshots:
        return
    lower, elo, upper = Elo(test.results())
    error = max(upper - elo, elo - lower)
    # Every game the same result (a sweep) has no variance, so the interval collapses to nothing, which is not certainty: it is
    # stored as no interval (null), and the pages and the CSV say so
    snapshots.update(check_elo=elo, check_error=error if error > 0 else None, check_games=test.games)


def averaged_values(run):

    # The result: per parameter the mean over the snapshots in the last `average_tail` of the iterations so far, and the
    # current value. Less noisy than the last values alone.
    current   = current_values(run)
    snapshots = list(run.snapshots.all())
    if not snapshots:
        return current

    so_far  = max(s.iteration for s in snapshots)
    from_it = so_far * (1.0 - run.average_tail)
    chosen  = [s for s in snapshots if s.iteration >= from_it and s.iteration > 0]

    out = {}
    for name, value in current.items():
        seen = [s.values[name] for s in chosen if name in s.values] + [value]
        out[name] = sum(seen) / len(seen)
    return out


def lately(run):

    # Per parameter, the share of its range it moved over the last 10% of the snapshots: |now - then| / (max - min)
    snapshots = list(run.snapshots.all())
    current   = current_values(run)
    if len(snapshots) < 2:
        return { name : 0.0 for name in current }

    back = snapshots[max(0, len(snapshots) - 1 - max(1, math.ceil(0.1 * len(snapshots))))]
    out  = {}
    for p in run.parameters.order_by('index'):
        span = p.max_value - p.min_value
        out[p.name] = abs(p.value - back.values.get(p.name, p.value)) / span if span else 0.0
    return out


def history(run):

    # The snapshot rows, for the running page's charts, JSON and CSV
    return [{
        'iteration' : s.iteration, 'games' : s.games, 'created' : s.created.isoformat(), 'values' : s.values,
        'check' : s.check_test_id, 'check_elo' : s.check_elo, 'check_error' : s.check_error, 'check_games' : s.check_games,
    } for s in run.snapshots.all()]


def history_csv(run):

    names = [p.name for p in run.parameters.order_by('index')]
    out   = io.StringIO()
    write = csv.writer(out, lineterminator='\n')
    write.writerow(['iteration', 'games', 'created'] + names + ['check', 'check_elo', 'check_error', 'check_games'])
    for row in history(run):
        write.writerow([row['iteration'], row['games'], row['created']] + [row['values'].get(n, '') for n in names]
                       + ['' if row[k] is None else row[k] for k in ('check', 'check_elo', 'check_error', 'check_games')])
    return out.getvalue()


def queue_sprt(tune, user=None, values=None):

    # An SPRT of `values` (the averaged ones) against the start: the same commit, the frozen values on both sides, the
    # engine's default bounds and confidence and the tune's time control. A plain test, made by the server, so it waits
    # for nothing.
    from . import create_test
    from .models import EngineConfig

    run     = tune.spsa_run
    values  = values if values is not None else averaged_values(run)
    config  = EngineConfig.objects.filter(name=tune.dev_engine).first()
    default = create_test.preset_values(config, 'default') if config else {}

    test, report = queue_test(tune, '%s SPRT' % (tune.name or 'Tune %d' % tune.id), values, start_values(run), author=user,
                              test_mode='SPRT', info='SPRT of the result of tune %d' % tune.id)
    create_test.apply_sprt(test, default.get('bounds', '[0.00, 5.00]'), default.get('confidence', '[0.05, 0.05]'))
    test.save()
    report()
    return test


def check_match_ids():

    # The matches of the outcome checks: tied to their tune, so the lists leave them out and the tune's page lists them
    return [Test(id=test_id, test_mode='GAMES').match_id() for test_id in
            SPSASnapshot.objects.exclude(check_test=None).values_list('check_test_id', flat=True).distinct()]


def list_figures(match_ids):

    # What a row of a list says of a tune instead of an Elo from its pairs, which hovers round zero: how far it is, and the
    # newest finished check's Elo. { match id : dict }
    from .ids import TEST_FORM
    from .models import SPSARun

    numbers = { int(found['number']): match_id for match_id in match_ids if (found := TEST_FORM.match(match_id)) and found['prefix'] == 'tune' }
    out = {}
    for run in SPSARun.objects.filter(tune_id__in=numbers).select_related('tune'):
        last = run.snapshots.exclude(check_elo=None).order_by('-iteration').first()
        out[numbers[run.tune_id]] = {
            'iteration': iteration_of(run, run.tune.games), 'iterations': run.iterations,
            'elo': last.check_elo if last else None, 'error': last.check_error if last else None, 'at': last.iteration if last else None,
        }
    return out
