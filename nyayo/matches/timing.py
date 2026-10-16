"""How far a gauntlet has got, how long a game takes and when it should be done.

Wall clock throughout: a game's duration is from its first move reaching nyayo to its result, and the estimate is the
pace the gauntlet has kept so far, so it follows however many games are played at once and however long they last.
It's an estimate: the end of a gauntlet is a tail of the last games and runs longer than the pace suggests.
"""

from datetime import timedelta

# a game that took longer than this (a runner paused, a resumed game) says nothing about how long games take
LONGEST_GAME = timedelta(hours=6)
# enough finished games for the overall pace to mean more than the first few
PACE_AFTER = 4
STALE_AFTER = timedelta(minutes=2)


def game_durations(rows):
    """Seconds each finished game took, from (started_at, finished_at) pairs; the ones that make no sense dropped."""
    seconds = []
    for started, finished in rows:
        if started is None or finished is None:
            continue
        taken = finished - started
        if timedelta(0) < taken <= LONGEST_GAME:
            seconds.append(taken.total_seconds())
    return seconds


def summarize(state, started_at, finished_at, updated_at, total, finished, playing, durations, concurrency, now):
    """The figures a match page shows above its results; None where there's nothing to say."""
    running = state == "running"
    end = now if running else (finished_at or updated_at or now)
    elapsed = max(0.0, (end - started_at).total_seconds()) if started_at else None
    average = sum(durations) / len(durations) if durations else None

    remaining = max(0, total - finished) if total else None
    per_hour = finished / elapsed * 3600 if elapsed and finished else None
    eta_seconds, basis = None, ""
    if running and remaining:
        if finished >= PACE_AFTER and per_hour:
            eta_seconds, basis = remaining / per_hour * 3600, "the pace so far"
        elif average and concurrency:
            eta_seconds, basis = remaining * average / concurrency, f"{concurrency} games at a time, "\
                                                                     f"{average:.0f} s each"
    stale = None
    if running and updated_at and now - updated_at > STALE_AFTER:
        stale = (now - updated_at).total_seconds()

    def percent(count):
        return 100.0 * count / total if total else 0.0

    return {
        "total": total, "finished": finished, "playing": playing, "remaining": remaining,
        "percent_finished": percent(finished), "percent_playing": percent(playing),
        "elapsed": elapsed, "average_game": average, "per_hour": per_hour, "eta_seconds": eta_seconds,
        "eta_at": now + timedelta(seconds=eta_seconds) if eta_seconds is not None else None,
        "eta_basis": basis, "stale_seconds": stale, "running": running, "sampled": len(durations),
    }
