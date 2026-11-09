import itertools


def test_steps_are_timed_with_the_monotonic_clock_so_a_wall_clock_jump_gives_no_negative_seconds(monkeypatch):
    import worker

    sent = []
    monkeypatch.setattr(worker.ServerReporter, 'report_step', lambda config, step, side, state, **fields: sent.append((step, state, fields)))
    ticks = itertools.count(100, 2)
    monkeypatch.setattr(worker.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(worker.time, 'time', lambda: 1000 - next(ticks))   # runs backwards: it would give a negative time

    try:
        worker.fail_running(None, 'it failed', 'the log', worker.time.monotonic())
    except worker.utils.OpenBenchRunnerFailedException:
        pass
    (step, state, fields), = sent
    assert (step, state) == ('running', 'failed') and fields['seconds'] >= 0
