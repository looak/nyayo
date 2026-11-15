# The worker's half of reading an engine's tunable parameters (nyayo/lab/tunables.py is the server's): the engine's own answer,
# turned into the lines of an SPSA input, `name, int|float, value, min, max, c_end, r_end`.
#
# The engine is asked `spsa` first. An engine that has the command (Elephant's tune build) prints those lines itself, and they are
# used as they are. One that has not is asked `uci`, and every `option name X type spin default D min A max B` it lists is a
# parameter, with c_end a twentieth of its range (1 at least) and r_end 0.002 guessed, the engine's ordinary options left out.

import queue
import re
import subprocess
import threading
import time

SPSA_WAIT = 10.0   # seconds to the end of an answer to `spsa` or `uci`
R_END     = '0.002'

NUMBER = r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?'
SPSA_LINE = re.compile(r'^([^\s=,]+)\s*,\s*(int|float)\s*,\s*(%s)\s*,\s*(%s)\s*,\s*(%s)\s*,\s*(%s)\s*,\s*(%s)\s*$' % ((NUMBER,) * 5))
SPIN = re.compile(r'^option\s+name\s+(.+?)\s+type\s+spin\s+default\s+(-?\d+)\s+min\s+(-?\d+)\s+max\s+(-?\d+)\s*$')


class TunablesError(Exception):
    """Reading failed: the message is what the server shows, with the last lines the engine said."""


def parse_spsa(output):
    """The lines of an answer to `spsa` that are `name, int|float, value, min, max, c_end, r_end`, normalised, in order."""
    found, seen = [], set()
    for line in output:
        match = SPSA_LINE.match(line.strip())
        if match and match.group(1) not in seen:
            seen.add(match.group(1))
            found.append(', '.join(match.groups()))
    return found


def parse_uci(output, ordinary=()):
    """A parameter for every spin option of an answer to `uci` that is not one of the engine's ordinary ones, c_end a twentieth of
    its range and r_end 0.002. An option whose name has a space, a comma or an equals sign cannot be a line of an SPSA input."""
    skip, found, seen = {name.lower() for name in ordinary}, [], set()
    for line in output:
        match = SPIN.match(line.strip())
        if not match:
            continue
        name, default, low, high = match.group(1), int(match.group(2)), int(match.group(3)), int(match.group(4))
        if name.lower() in skip or name in seen or not re.fullmatch(r'[^\s=,]+', name) or low >= high or not low <= default <= high:
            continue
        seen.add(name)
        found.append('%s, int, %d, %d, %d, %d, %s' % (name, default, low, high, max(1, round((high - low) / 20)), R_END))
    return found


def ask(binary, command, until, wait=SPSA_WAIT):
    """The lines an engine prints to a command, until one is `until` (or it is over, or `wait` seconds are). A command the engine
    does not know is no answer, and an `isready` after it makes the end certain."""
    process = subprocess.Popen([binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               errors='replace', bufsize=1)
    lines = queue.Queue()

    def reader():
        for line in process.stdout:
            lines.put(line.rstrip('\r\n'))
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    got, deadline = [], time.monotonic() + wait
    try:
        process.stdin.write(command + '\n')
        process.stdin.flush()
        while (left := deadline - time.monotonic()) > 0:
            try: line = lines.get(timeout=left)
            except queue.Empty: break
            if line is None:
                break
            got.append(line)
            if line.strip() == until:
                break
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            process.stdin.write('quit\n')
            process.stdin.flush()
        except Exception:
            pass
        try: process.wait(timeout=1)
        except Exception:
            process.kill()
    return got


def tail(lines, count=20):
    return '\n'.join(lines[-count:])


def read(binary, ordinary=(), wait=SPSA_WAIT):
    """(command, lines): what the engine at `binary` says its tunable parameters are, from `spsa` if it answers, else from `uci`.
    Raises TunablesError, with the engine's last lines, when it gives none."""
    answer = ask(binary, 'spsa\nisready', 'readyok', wait)
    if lines := parse_spsa(answer):
        return 'spsa', lines

    answer_uci = ask(binary, 'uci', 'uciok', wait)
    if lines := parse_uci(answer_uci, ordinary):
        return 'uci', lines

    said = answer_uci or answer
    if any(line.strip() == 'uciok' for line in answer_uci):
        raise TunablesError('The engine answered no spsa command, and its uci answer has no spin option that is not an ordinary one.\n' + tail(said))
    raise TunablesError('The engine answered neither spsa nor uci with anything to use.\n' + tail(said))
