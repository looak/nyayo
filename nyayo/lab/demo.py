"""Two workers for polishing the worker page, made by `manage.py seed_demo` (debug mode only; the numbers are invented).

pc-wsl has the numbers of the mock's board (project/WorkerB.dc.html in the design project), so the page can be set beside it: its
48 hours, its hardware, flags, toolchains and software. new-box is a worker that has been heard from for two hours."""

import secrets
from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone

from nyayo.lab import hours
from nyayo.lab.models import Engine, Machine, MachineHour, Result, SPSAParameter, Test

GAMES = [92, 88, 95, 101, 97, 90, 84, 0, 0, 0, 76, 88, 93, 99, 104, 96, 91, 87, 82, 90, 98, 102, 95, 89, 86, 91, 0, 0, 72, 84, 90, 96,
         101, 108, 99, 94, 88, 85, 92, 97, 103, 99, 94, 90, 87, 93, 96, 58]
MS = [1.1, 1.0, 1.2, 1.1, 1.0, 1.3, 1.1, 0, 0, 0, 2.4, 1.4, 1.1, 1.0, 1.1, 1.2, 1.0, 1.1, 1.0, 1.1, 1.2, 1.0, 1.1, 1.1, 1.0, 1.2, 0, 0,
      3.1, 1.5, 1.2, 1.1, 1.0, 1.0, 1.1, 1.2, 1.1, 1.0, 1.1, 1.0, 1.2, 1.1, 1.0, 1.1, 1.0, 1.1, 1.0, 1.0]
FLAGS = ['POPCNT', 'BMI2', 'SSSE3', 'SSE41', 'SSE42', 'SSE4A', 'AVX', 'AVX2', 'FMA', 'AVX512F', 'AVX512BW', 'AVX512DQ', 'AVX512VNNI']
TOOLCHAINS = {'g++': '13.3.0', 'clang++': '21.1.4', 'gcc': '13.3.0', 'clang': '21.1.4', 'python3': '3.12.3', 'rustc': None, 'cargo': None,
              'dotnet': None, 'make': '4.3'}
TESTS = (26, 3, 2)  # SPRTs, SPSA tunes and plain games, 31 tests
TOTAL_GAMES = 14208


def machine(name, now, **info):
    row = Machine.objects.filter(name=name).first()
    if row is None:
        row = Machine.objects.create(name=name, token_hash=secrets.token_hex(32), state='approved', enabled=True, described=True, secret='s' * 8, info=info)
    row.info = info
    row.state, row.enabled, row.described = 'approved', True, True
    row.save()
    return row


def seed(now=None):
    now = now or timezone.now()
    this = hours.start_of(now)
    for name in ('pc-wsl', 'new-box'):
        MachineHour.objects.filter(box=name).delete()

    wsl = machine('pc-wsl', now, client_ver=55, concurrency=10, sockets=1, physical_cores=12, logical_cores=24, syzygy_max=0, cpu_flags=FLAGS,
                  toolchains=TOOLCHAINS, tokens={}, os_name='Linux', os_ver='6.18.54-microsoft-standard-WSL2', supported=['Elephant'],
                  cpu_name='AMD Ryzen 9 9900X', isa_name='x86-64-avx512icl', ram_total_mb=31 * 1024, huge_pages='madvise',
                  gpus=['NVIDIA GeForce RTX 4080 SUPER'], python_ver='3.12.3', fastchess_ver='1.8.2')
    for i, (games, ms) in enumerate(zip(GAMES, MS)):
        online = ms > 0
        MachineHour.objects.create(box='pc-wsl', hour=this - hours.HOUR * (47 - i), games=games, heard=3600 if online else 0,
                                   rtt_sum=ms, rtt_n=1 if online else 0)
    Machine.objects.filter(pk=wsl.pk).update(updated=now - timedelta(seconds=12), first_seen=now - timedelta(days=11), rtt_ms=1.0)

    # its tests: 31, the last of them the one it is on
    owner = User.objects.get_or_create(username='owner')[0]
    dev, _ = Engine.objects.get_or_create(name='feature', sha='a' * 40, defaults=dict(source='https://github.com/looak/elephant', bench=1234))
    base, _ = Engine.objects.get_or_create(name='main', sha='b' * 40, defaults=dict(source='https://github.com/looak/elephant', bench=1000))
    Result.objects.filter(machine=wsl).delete()
    Test.objects.filter(name__startswith='demo-box-').delete()
    Test.objects.filter(name='eval-pieces-outposts').delete()
    modes = ['SPRT'] * TESTS[0] + ['SPSA'] * TESTS[1] + ['GAMES'] * TESTS[2]
    for n, mode in enumerate(modes):
        last = n == len(modes) - 1
        test = Test.objects.create(author=owner, dev=dev, base=base, dev_repo=dev.source, base_repo=base.source, dev_engine='Elephant', base_engine='Elephant',
                                   dev_options='Threads=1 Hash=8', base_options='Threads=1 Hash=8', dev_time_control='8.0+0.08', base_time_control='8.0+0.08',
                                   book_name='8moves_v3.epd', workload_size=4, throughput=100, elolower=0.0, eloupper=5.0, alpha=0.05, beta=0.05,
                                   lowerllr=-2.94, upperllr=2.94, approved=True, test_mode=mode, finished=not last,
                                   name='eval-pieces-outposts' if last else 'demo-box-%d' % (n + 1))
        games = 32 if last else (TOTAL_GAMES - 32) // (len(modes) - 1) + (16 if n == 0 else 0)
        result = Result.objects.create(test=test, machine=wsl, games=games, workloads=7 if last else 20, timeloss=2 if n == 0 else 0)
        Result.objects.filter(pk=result.pk).update(updated=now - timedelta(minutes=9) if last else now - timedelta(hours=30 + n))

    # a worker new to nyayo: two hours of history
    new = machine('new-box', now, client_ver=55, concurrency=4, sockets=1, physical_cores=4, logical_cores=8, syzygy_max=0, cpu_flags=FLAGS[:8],
                  toolchains=TOOLCHAINS, tokens={}, os_name='Linux', os_ver='6.8.0', supported=['Elephant'], cpu_name='Intel Core i7-8700',
                  isa_name='x86-64-avx2', ram_total_mb=16 * 1024, python_ver='3.12.3', fastchess_ver='1.8.2')
    for ago, games, ms in ((1, 41, 1.4), (0, 12, 1.2)):
        MachineHour.objects.create(box='new-box', hour=this - hours.HOUR * ago, games=games, heard=3000, rtt_sum=ms, rtt_n=1)
    Machine.objects.filter(pk=new.pk).update(updated=now - timedelta(seconds=20), first_seen=now - timedelta(hours=2), rtt_ms=1.2)
    return ['/machines/pc-wsl', '/machines/new-box']


TUNE_PARAMS = [  # name, start, min, max, c_end, where it heads (share of the range), how fast it settles
    ('LmrBase', 75, 25, 150, 6, 0.10, 0.9), ('LmrDivisor', 225, 100, 400, 15, -0.08, 0.7), ('NmpBase', 3, 1, 6, 0.5, 0.0, 1.0),
    ('NmpDepthDivisor', 4, 2, 8, 0.5, 0.06, 0.8), ('RfpMargin', 80, 30, 200, 8, -0.09, 0.6), ('RfpMaxDepth', 7, 3, 12, 0.5, 0.05, 1.0),
    ('FutilityMargin', 100, 40, 250, 10, 0.12, 0.35), ('LmpBase', 3, 1, 8, 0.5, 0.02, 1.0),
    ('AspirationWindow', 25, 8, 60, 3, -0.11, 0.8), ('SeeQuietMargin', -60, -150, -10, 6, 0.07, 0.5),
    ('HistoryDivisor', 8192, 2048, 16384, 600, -0.04, 0.9), ('SingularMargin', 2, 1, 5, 0.25, 0.03, 0.7)]


def seed_tune(now=None, name='demo-search-params', iterations=20000, now_iteration=7420, finished=False):
    """A tune with the numbers of the design board's running page (TuneRunning.dc.html): snapshots every 100 iterations, a check
    every 1000 with its Elo, and, for a finished one, the SPRT queued after it. Invented, like the rest."""
    import random

    from nyayo.lab import document, spsa_utils, tunes
    from nyayo.lab.models import SPSARun, SPSASnapshot

    now = now or timezone.now()
    Test.objects.filter(name=name).delete()
    owner = User.objects.get_or_create(username='owner')[0]
    dev, _ = Engine.objects.get_or_create(name='search-params', sha='3f1c9a2' + '0' * 33, defaults=dict(source='https://github.com/looak/elephant', bench=5732794))
    test = Test.objects.create(author=owner, dev=dev, base=dev, dev_repo=dev.source, base_repo=dev.source, dev_engine='Elephant', base_engine='Elephant',
                               dev_options='Threads=1 Hash=8 SingularDepth=6 IirDepth=4', base_options='Threads=1 Hash=8 SingularDepth=6 IirDepth=4',
                               dev_time_control='8.0+0.08', base_time_control='8.0+0.08', book_name='8moves_v3.epd', workload_size=4, throughput=100,
                               approved=True, test_mode='SPSA', name=name)
    post = {'spsa_alpha': '0.602', 'spsa_gamma': '0.101', 'spsa_A_ratio': '0.1', 'spsa_iterations': str(iterations), 'spsa_pairs_per': '2',
            'spsa_reporting_type': 'BULK', 'spsa_distribution_type': 'SINGLE', 'spsa_check_every': '1000', 'spsa_snapshot_every': '100', 'spsa_then_sprt': finished,
            'spsa_inputs': '\n'.join('%s, int, %s, %s, %s, %s, 0.002' % (n, s, lo, hi, c) for n, s, lo, hi, c, _, _ in TUNE_PARAMS),
            'spsa_frozen_list': [{'name': 'SingularDepth', 'value': 6}, {'name': 'IirDepth', 'value': 4}]}
    run = spsa_utils.create_spsa_run(test, type('Form', (), {'POST': post})())
    run.snapshots.all().delete()

    rng = random.Random(1003)
    games = lambda it: it * 2 * run.pairs_per
    series = {}
    for n, start, lo, hi, c, aim, speed in TUNE_PARAMS:
        value, vals = float(start), [float(start)]
        for i in range(1, now_iteration // 100 + 1):
            t = i / (now_iteration // 100)
            value += (start + aim * (hi - lo) - value) * 0.06 * speed + rng.gauss(0, 1) * (hi - lo) * 0.012 * (1.15 - t * 0.6)
            value = max(lo, min(hi, value))
            vals.append(value)
        series[n] = vals
    for i in range(now_iteration // 100 + 1):
        snap = SPSASnapshot.objects.create(spsa_run=run, iteration=i * 100, games=games(i * 100), values={n: v[i] for n, v in series.items()})
        SPSASnapshot.objects.filter(pk=snap.pk).update(created=now - timedelta(minutes=(now_iteration // 100 - i) * 11))
        if i * 100 and i * 100 % 1000 == 0:
            check = tunes.make_check(test, run, snap)
            elo = 1.8 * (i * 100 / 1000) ** 0.9 + rng.gauss(0, 0.6)
            Test.objects.filter(pk=check.pk).update(finished=True, games=500, wins=250, losses=200, draws=50)
            SPSASnapshot.objects.filter(pk=snap.pk).update(check_elo=elo, check_error=15.0 + rng.gauss(0, 0.2), check_games=500)
    for param in run.parameters.all():
        SPSAParameter.objects.filter(pk=param.pk).update(value=series[param.name][-1])
    g = games(now_iteration)
    Test.objects.filter(pk=test.pk).update(games=g, wins=g * 3 // 10, draws=g - g * 3 // 10 - g * 3 // 10, losses=g * 3 // 10,
                                           DD=g // 2 - g * 3 // 20 * 2, LD=g * 3 // 20, DW=g * 3 // 20, finished=finished, passed=finished)
    if machine := Machine.objects.filter(name='pc-wsl').first():
        g = games(now_iteration)
        result = Result.objects.create(test=test, machine=machine, games=g, wins=g * 3 // 10, draws=g - g * 3 // 10 - g * 3 // 10, losses=g * 3 // 10,
                                       DD=g // 2 - g * 3 // 20 * 2, LD=g * 3 // 20, DW=g * 3 // 20, workloads=now_iteration // 2)
        Result.objects.filter(pk=result.pk).update(updated=now)
    test = Test.objects.get(pk=test.pk)
    if finished:
        sprt = tunes.queue_sprt(test)
        run.then_test = sprt
        run.save(update_fields=['then_test'])
        document.report_test(sprt)
    for check_test in Test.objects.filter(tune_snapshots__spsa_run=run):
        document.report_test(check_test)
    document.report_test(test)
    return '/matches/%s' % test.match_id()
