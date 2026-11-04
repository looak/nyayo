"""How long the network takes between nyayo and a worker, as the kernel measures it: the round-trip estimate of the TCP
connection a worker's call came in on (`tcpi_rtt` of Linux's TCP_INFO, microseconds, an unsigned int at byte 68 of the
struct). gunicorn puts that connection in the request's environ as `gunicorn.socket`.

It is the network's round trip, not nyayo's own processing time, and behind a reverse proxy it would be the proxy's
(docs/deploy.md, *Behind a reverse proxy later*). It is read in `clientGetWorkload` and `clientHeartbeat`, and it never
fails a request: anything missing (runserver, Windows, a closed socket, a kernel that says 0, a struct too short) leaves
the machine as it was. `note` sets `rtt_ms` on the machine; `changed` names the field for the write that follows."""

import socket
import struct

TCP_INFO = getattr(socket, 'TCP_INFO', 11)   # Linux's number, for where Python does not name it; Windows has no such option
TCP_INFO_BYTES = 104
RTT_OFFSET = 68      # tcpi_rtt, after the 8 single bytes and 15 unsigned ints before it
LARGEST_MS = 3600 * 1000


def measure(sock):
    """The round trip of a connection in ms, or None."""
    try:
        data = sock.getsockopt(socket.IPPROTO_TCP, TCP_INFO, TCP_INFO_BYTES)
        micro = struct.unpack_from('=I', data, RTT_OFFSET)[0]
    except Exception:
        return None
    return micro / 1000 if 0 < micro <= LARGEST_MS * 1000 else None


def note(request, machine):
    machine.rtt_noted = False
    try:
        sock = request.META.get('gunicorn.socket')
    except Exception:
        return
    if sock is None:
        return
    value = measure(sock)
    if value is not None:
        machine.rtt_ms = value
        machine.rtt_noted = True


def changed(machine):
    return ['rtt_ms'] if getattr(machine, 'rtt_noted', False) else []


def values(machine):
    return {name: getattr(machine, name) for name in changed(machine)}
