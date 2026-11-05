"""Two workers for polishing the worker page, made by `manage.py seed_demo` (debug mode only; the numbers are invented).

pc-wsl has the numbers of the mock's board (project/WorkerB.dc.html in the design project), so the page can be set beside it: its
48 hours, its hardware, flags, toolchains and software. new-box is a worker that has been heard from for two hours."""

import secrets
from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone

from nyayo.lab import hours
from nyayo.lab.models import Engine, Machine, MachineHour, Result, Test

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

    wsl = machine('pc-wsl', now, client_ver=54, concurrency=10, sockets=1, physical_cores=12, logical_cores=24, syzygy_max=0, cpu_flags=FLAGS,
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
    new = machine('new-box', now, client_ver=54, concurrency=4, sockets=1, physical_cores=4, logical_cores=8, syzygy_max=0, cpu_flags=FLAGS[:8],
                  toolchains=TOOLCHAINS, tokens={}, os_name='Linux', os_ver='6.8.0', supported=['Elephant'], cpu_name='Intel Core i7-8700',
                  isa_name='x86-64-avx2', ram_total_mb=16 * 1024, python_ver='3.12.3', fastchess_ver='1.8.2')
    for ago, games, ms in ((1, 41, 1.4), (0, 12, 1.2)):
        MachineHour.objects.create(box='new-box', hour=this - hours.HOUR * ago, games=games, heard=3000, rtt_sum=ms, rtt_n=1)
    Machine.objects.filter(pk=new.pk).update(updated=now - timedelta(seconds=20), first_seen=now - timedelta(hours=2), rtt_ms=1.2)
    return ['/machines/pc-wsl', '/machines/new-box']
