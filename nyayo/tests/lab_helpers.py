"""Makes a test, a machine and a result the way the shell does, for the lab's tests."""

import secrets

from django.contrib.auth.models import User

from nyayo.lab.models import Engine, Machine, Result, Test


def make_test(**fields):
    user = User.objects.get_or_create(username="owner")[0]
    dev = Engine.objects.create(name="feature", source="https://github.com/looak/elephant", sha="a" * 40, bench=1234)
    base = Engine.objects.create(name="main", source="https://github.com/looak/elephant", sha="b" * 40, bench=1000)
    values = dict(author=user, dev=dev, base=base, dev_repo=dev.source, base_repo=base.source, dev_engine="Elephant",
                  base_engine="Elephant", dev_options="Threads=1 Hash=8", base_options="Threads=1 Hash=8",
                  dev_time_control="8.0+0.08", base_time_control="8.0+0.08", book_name="8moves_v3.epd",
                  workload_size=4, throughput=100, elolower=0.0, eloupper=5.0, alpha=0.05, beta=0.05, lowerllr=-2.94, upperllr=2.94,
                  approved=True)
    values.update(fields)
    return Test.objects.create(**values)


def make_machine(name="hunt", state="approved", enabled=True, described=True, **info):
    """An approved worker, as one that said hello and described itself is: a row of its own, named, with a token's hash."""
    base = dict(client_ver=54, concurrency=2, sockets=1, physical_cores=2, syzygy_max=0,
                cpu_flags=["POPCNT"], toolchains={"g++": "13.3.0", "gcc": "13.3.0", "make": "4.3"}, tokens={}, os_name="Linux",
                supported=["Elephant"])
    base.update(info)
    return Machine.objects.create(name=name, token_hash=secrets.token_hex(32), state=state, enabled=enabled, described=described,
                                  info=base, secret="s" * 8)


def make_result(test, machine):
    return Result.objects.create(test=test, machine=machine, workloads=1)
