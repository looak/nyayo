# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                             #
#   OpenBench is a chess engine testing framework authored by Andrew Grant.   #
#   <https://github.com/AndyGrant/OpenBench>           <andrew@grantnet.us>   #
#                                                                             #
#   OpenBench is free software: you can redistribute it and/or modify         #
#   it under the terms of the GNU General Public License as published by      #
#   the Free Software Foundation, either version 3 of the License, or         #
#   (at your option) any later version.                                       #
#                                                                             #
#   OpenBench is distributed in the hope that it will be useful,              #
#   but WITHOUT ANY WARRANTY; without even the implied warranty of            #
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the             #
#   GNU General Public License for more details.                              #
#                                                                             #
#   You should have received a copy of the GNU General Public License         #
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.     #
#                                                                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #


# Adapted for nyayo from OpenBench's OpenBench/utils.py (openbench_eg da629d7): the time control, the active tests,
# the machines heard from lately, update_test (which applies a workload's results), and the helpers the workload
# pages use. The logic is OpenBench's; what read OpenBench's config files (git credentials) reads nyayo's settings.
# What the pages, the networks, books and engines administration of that file did comes with the pages in later
# steps. Profile is gone, and a report to nyayo is report_test, in this process.

import datetime
import json
import re

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from nyayo.lab import hours
from nyayo.lab.document import report_test
from nyayo.lab.models import Machine, Result, SPSAParameter, Test
from nyayo.lab.stats import TrinomialSPRT, PentanomialSPRT


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

        print ('FAIL')
        import sys
        sys.stdout.flush()

        raise Exception('Unable to parse Time Control (%s)' % (time_str))

    @staticmethod
    def control_type(time_str):

        # Return one of the types defined at the top of TimeControl

        if time_str.startswith('N='):
            return TimeControl.FIXED_NODES

        if time_str.startswith('D='):
            return TimeControl.FIXED_DEPTH

        if time_str.startswith('MT='):
            return TimeControl.FIXED_TIME

        if '/' in time_str:
            return TimeControl.CYCLIC

        return TimeControl.FISCHER

    @staticmethod
    def control_base(time_str):

        # Fixed-nodes, Fixed-depth, Fixed-time
        if '=' in time_str:
            return int(time_str.split('=')[1])

        # Cyclic
        if '/' in time_str:
            return float(time_str.split('/')[0])

        # Fischer or Sudden Death otherwise
        return float(time_str.split('+')[0])

def workload_uses_time_based_tc(workload):

    dev_type  = TimeControl.control_type(workload.dev_time_control)
    base_type = TimeControl.control_type(workload.base_time_control)

    return  workload.upload_pgns == 'VERBOSE' \
       or (dev_type  != TimeControl.FIXED_NODES and dev_type  != TimeControl.FIXED_DEPTH) \
       or (base_type != TimeControl.FIXED_NODES and base_type != TimeControl.FIXED_DEPTH)

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


def get_active_tests():
    t = Test.objects.select_related('dev', 'base').filter(approved=True)
    t = t.exclude(finished=True)
    t = t.exclude(deleted=True)
    return t.order_by('-priority', '-currentllr')

def getRecentMachines(minutes=2):
    target = timezone.now() - datetime.timedelta(minutes=minutes)
    return Machine.objects.filter(updated__gte=target)

def update_test(request, machine):

    # Extract error information
    crashes    = int(request.POST['crashes'   ])
    timelosses = int(request.POST['timelosses'])
    illegals   = int(request.POST['illegals'  ])

    # Extract Database information
    machine_id = int(request.POST['machine_id'])
    result_id  = int(request.POST['result_id' ])
    test_id    = int(request.POST['test_id'   ])

    # Trinomial Implementation
    losses, draws, wins = map(int, request.POST['trinomial'].split())
    games = losses + draws + wins

    # Pentanomial Implementation
    LL, LD, DD, DW, WW = map(int, request.POST['pentanomial'].split())

    # SPSA Delta update vector; might not have this
    raw_spsa_delta = request.POST.get('spsa_delta', '')
    spsa_delta     = json.loads(raw_spsa_delta) if raw_spsa_delta else []

    with transaction.atomic():

        # MASSIVE risk for concurrent access to the Test. select_for_update() will lock the row,
        # which correctly ensures no other entity can modify it. HOWEVER, spsa_run and the various
        # spsa_run.parameters are NOT locked via this query. This is okay because no other location
        # in OpenBench would be modifying the contents of those models.
        #
        # ALL of the updates here, even the trivial ones to the Result and Machine, are wrapped in
        # same transaction.atomic(). The sole purpose and utility of that is to ensure either EVERYTHING
        # gets updated as per this function, or NOTHING gets updated.

        test = Test.objects.select_for_update().filter(id=test_id).first()

        if test is None or test.finished or test.deleted or not test.approved: # Gone (deleted with its games), stopped, or unapproved # Unapproved, a worker on it is told to stop
            return { 'stop' : True }

        test.losses += losses # Trinomial
        test.draws  += draws
        test.wins   += wins
        test.LL     += LL     # Pentanomial
        test.LD     += LD
        test.DD     += DD
        test.DW     += DW
        test.WW     += WW
        test.games  += games  # Overall

        # Consider only Crashes or Illegal moves as real errors
        test.error = bool(test.error or crashes or illegals)

        if test.test_mode == 'SPRT':

            # Compute a new LLR for the updated results ( Penta )
            if test.use_penta:
                results = (test.LL, test.LD, test.DD, test.DW, test.WW)
                test.currentllr = PentanomialSPRT(results, test.elolower, test.eloupper)

            # Compute a new LLR for the updated results ( Tri )
            elif test.use_tri:
                results = (test.losses, test.draws, test.wins)
                test.currentllr = TrinomialSPRT(results, test.elolower, test.eloupper)

            # Check for H0 or H1 being accepted
            test.passed   = test.currentllr > test.upperllr
            test.failed   = test.currentllr < test.lowerllr
            test.finished = test.passed or test.failed

        elif test.test_mode == 'GAMES':

            # Finish test once we've played the proper amount of games
            test.passed   = test.games >= test.max_games and test.wins >= test.losses
            test.failed   = test.games >= test.max_games and test.wins <  test.losses
            test.finished = test.passed or test.failed

        elif test.test_mode == 'SPSA':

            # Apply updates to every Parameter, ensuring clipping
            parameters = list(test.spsa_run.parameters.order_by('index'))
            for delta, param in zip(spsa_delta, parameters):
                param.value = max(param.min_value, min(param.max_value, param.value + delta))

            # Bulk update to fire off all the .save()s
            SPSAParameter.objects.bulk_update(parameters, ['value'])

            test.finished = test.games >= 2 * test.spsa_run.pairs_per * test.spsa_run.iterations

        elif test.test_mode == 'DATAGEN':

            # Finished, and always passing, for a completed DATAGEN Workload
            test.passed = test.finished = test.games >= test.max_games

        test.save()

        # Update Result object; No risk from concurrent access
        Result.objects.filter(id=result_id).update(
            games    = F('games'   ) + games,
            losses   = F('losses'  ) + losses,
            draws    = F('draws'   ) + draws,
            wins     = F('wins'    ) + wins,
            LL       = F('LL'      ) + LL,
            LD       = F('LD'      ) + LD,
            DD       = F('DD'      ) + DD,
            DW       = F('DW'      ) + DW,
            WW       = F('WW'      ) + WW,
            crashes  = F('crashes' ) + crashes,
            timeloss = F('timeloss') + timelosses,
            updated  = timezone.now()
        )

        # Update Machine object; No meaningful risk from concurrent access
        Machine.objects.filter(id=machine_id).update(
            updated=timezone.now()
        )

    # The hour of the box's graph, outside the transaction: a failure there must not undo the results
    hours.note(machine, games=games)

    # Reported once the Result is updated too: the report's counts are the Result rows' sums
    transaction.on_commit(lambda: report_test(test.id))

    return [{}, { 'stop' : True }][test.finished]
