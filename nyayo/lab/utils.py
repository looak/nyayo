
# Adapted for nyayo from OpenBench's OpenBench/utils.py (openbench_eg da629d7): the helpers the workload pages use. The
# logic is OpenBench's; what read OpenBench's config files (git credentials) reads nyayo's settings.

import re


class TimeControl(object):

    FIXED_NODES = 'FIXED-NODES' # N= or nodes=
    FIXED_DEPTH = 'FIXED-DEPTH' # D= or depth=
    FIXED_TIME  = 'FIXED-TIME'  # MT= or movetime=
    CYCLIC      = 'CYCLIC'      # X/Y or X/Y+Z
    FISCHER     = 'FISCHER'     # Y or Y+Z

    @staticmethod
    def parse(time_str):

        # Display Nodes as N=, Depth as D=, MoveTime as MT=
        conversion = {
            'N'  :  'N', 'nodes'    :  'N',
            'D'  :  'D', 'depth'    :  'D',
            'MT' : 'MT', 'movetime' : 'MT',
        }

        # Searching for "nodes=", "depth=", and "movetime=" time controls
        pattern = r'(?P<mode>((N)|(D)|(MT)|(nodes)|(depth)|(movetime)))=(?P<value>(\d+))'
        if results := re.search(pattern, time_str.upper()):
            mode, value = results.group('mode', 'value')
            return '%s=%s' % (conversion[mode], value)

        # Searching for "X/Y+Z" time controls, where "X/" is optional
        pattern = r'(?P<moves>(\d+/)?)(?P<base>\d*(\.\d+)?)(?P<inc>\+(\d+\.)?\d+)?'
        if results := re.search(pattern, time_str):
            moves, base, inc = results.group('moves', 'base', 'inc')

            # Strip the trailing and leading symbols
            moves = None if moves == '' else moves.rstrip('/')
            inc   = 0.0  if inc   is None else inc.lstrip('+')

            # Format the time control for match runner cleanly
            if moves is None: return '%.1f+%.2f' % (float(base), float(inc))
            return '%d/%.1f+%.2f' % (int(moves), float(base), float(inc))

        raise Exception('Unable to parse Time Control (%s)' % (time_str))

def path_join(*args):
    return "/".join([f.lstrip("/").rstrip("/") for f in args]).rstrip('/')

def always_upload(upload_pgns):
    # The fork keeps every game for training, so FALSE (upstream's default) becomes COMPACT
    return 'COMPACT' if upload_pgns == 'FALSE' else upload_pgns

def extract_option(options, option):

    match = re.search(r'(?<={0}=")[^"]*'.format(option), options)
    if match: return match.group()

    match = re.search(r'(?<={0}=\')[^\']*'.format(option), options)
    if match: return match.group()

    match = re.search(r'(?<={0}=)[^ ]*'.format(option), options)
    if match: return match.group()

def determine_bench(typed, message):

    # Use the provided bench if possible
    try: return int(typed)
    except: pass

    # Fallback to try to parse the Bench from the commit
    try:
        benches = re.findall('(?:BENCH|NODES)[ :=]+([0-9,]+)', message, re.IGNORECASE)
        return int(benches[-1].replace(',', ''))
    except: return None

def determine_info(typed, message, dev_engine, base_engine, dev_branch, base_branch):

    # Use the provided info if possible
    if typed:
        return typed

    # Do not auto populate when comparing different engines, or the same branch
    if dev_engine != base_engine:
        return ''

    if dev_branch == base_branch:
        return ''

    # Strip the bench from the commit message, and tidy the whitespace
    lines = message.strip().split("\n")
    bench_search = re.compile(r"(^|\s)[Bb]ench[ :]+([1-9]\d{5,7})(?!\d)")
    for i, line in enumerate(reversed(lines)):
        new_line, n = bench_search.subn("", line)
        if n:
            lines[-i - 1] = new_line
            break
    s = "\n".join(lines)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n+", r"\n", s)
    return s.rstrip()
