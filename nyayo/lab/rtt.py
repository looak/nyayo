"""How long a worker's calls take, as the worker measured them (`rtt_ms` in the form of clientGetWorkload,
clientHeartbeat and clientSubmitResults). A worker that does not send it, or sends nonsense, leaves the machine as it
was. The values go in the writes the calls make already: `note` sets them on the machine, `changed` names the fields."""

AVERAGE_OF = 5        # the average weighs the latest call 1 in this many
LARGEST = 3600 * 1000 # an hour in ms, beyond which a value is a mistake


def note(request, machine):
    try:
        value = float(request.POST.get('rtt_ms'))
    except (TypeError, ValueError):
        return
    if not 0 <= value <= LARGEST or value != value:
        return
    machine.rtt_ms = value
    previous = machine.rtt_avg_ms
    machine.rtt_avg_ms = value if previous is None else previous + (value - previous) / AVERAGE_OF
    machine.rtt_noted = True


def changed(machine):
    return ['rtt_ms', 'rtt_avg_ms'] if getattr(machine, 'rtt_noted', False) else []


def values(machine):
    return {name: getattr(machine, name) for name in changed(machine)}
