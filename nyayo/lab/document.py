# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
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
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

# A test's match document (docs/match-schema.md, schema v1), built from the test and ingested in-process.
#
# Adapted from openbench_eg's OpenBench/nyayo.py (da629d7), the fork's reporter: its HTTP, queues, retries and token are
# gone, and so is the older document shape. What stays is the building of the document, which is the contract: the
# same producer, the same states, steps and verdict as the fork sent, so nothing fixed under a match id changes.

import collections
import datetime
import json
import logging
import re

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from nyayo.lab import steps as walk
from nyayo.lab.ids import match_id
from nyayo.lab.models import LogEvent, Result, Step, Test
from nyayo.lab.stats import Elo

logger = logging.getLogger(__name__)

# What the runner is told to write into a move's comment (the worker's pgnout_settings), as nyayo's pgn_options
PGN_OPTIONS = { 'fastchess' : ['nodes', 'seldepth'] }


def instance():
    return settings.NYAYO_INSTANCE


def match_state(test):
    # (state, sprt.result), derived from the test every time (ramani plans/sprt-steps.md, What each verdict does). A
    # test stopped on an engine's failure (the verdict, in the steps) is a failed start when nothing was played, an
    # error when games were. A test is waiting until a worker has reached running, the last step of its walk, or has
    # played a game, which a test from before the steps did without reporting one; held when a failure nobody can
    # place has left no worker able to play it.
    if test.deleted:
        return 'stopped', None
    if test.finished and (test.passed or test.failed):
        if test.test_mode != 'SPRT':
            return 'finished', None
        return 'finished', 'passed' if test.passed else 'failed'
    if test.finished:
        if walk.engine_failure(test):
            return ('error' if walk.games_played(test) else 'failed_prechecks'), None
        return 'stopped', None
    if walk.held(test):
        return 'held', None
    if not (Step.objects.filter(result__test=test, step='running', state='passed').exists()
            or Result.objects.filter(test=test, games__gt=0).exists()):
        return 'waiting', None
    return 'running', None


def ended_at(test, state, rows):
    # When a test that isn't running ended. One that passes, fails or runs out of games ends inside
    # update_test with no event, on the result that tipped it (later ones are refused), so at its newest
    # result; one stopped or deleted by hand at its newest STOP or DELETE; one stopped on an engine's failure at the
    # event its verdict wrote.
    # Not the newest event: that may be the CREATE, or a worker's crash in the middle of the run.
    events = LogEvent.objects.filter(test_id=test.id).order_by('-id')
    if state == 'finished':
        event = None
    elif state in ('error', 'failed_prechecks'):
        event = events.filter(summary__startswith=walk.STOPPED).first()
    else:
        event = events.filter(machine_id=0, summary__in=['STOP', 'DELETE']).first()
    if event:
        return event.created
    return max((row.updated for row in rows), default=None) or test.updated


def iso(when):
    return when.isoformat().replace('+00:00', 'Z') if when else None


def uci_options(options):
    # "Threads=1 Hash=16" as { "Threads": 1, "Hash": 16 }
    parsed = {}
    for name, value in re.findall(r'''([^\s=]+)=("[^"]*"|'[^']*'|\S+)''', options or ''):
        value = value.strip('"\'')
        parsed[name] = int(value) if re.fullmatch(r'-?\d+', value) else value
    return parsed


def player_name(engine, branch):
    # What nyayo's pages call a side of the test, and its games' players: the engine and the branch
    return '%s %s' % (engine, branch)


def build_document(test):
    # nyayo's match schema v1, as its docs/match-schema.md (OpenBench) maps a test

    rows  = list(Result.objects.filter(test=test).select_related('machine', 'machine__user').order_by('updated'))
    count = lambda name: sum(getattr(row, name) for row in rows)

    state, sprt_result = match_state(test)

    finished_at = iso(ended_at(test, state, rows)) if state not in ('waiting', 'held', 'running') else None

    sprt = stats = None
    if test.test_mode == 'SPRT':
        model = 'penta' if test.use_penta else 'tri'
        sprt  = {
            'elo0': test.elolower, 'elo1': test.eloupper, 'alpha': test.alpha, 'beta': test.beta, 'model': model,
            'llr': test.currentllr, 'lower': test.lowerllr, 'upper': test.upperllr, 'result': sprt_result,
        }
        lower, elo, upper = Elo(test.results())
        stats = { 'elo': elo, 'error': max(upper - elo, elo - lower), 'model': model }

    # A machine is on the test now when its workload is this test and it was heard from in the last 5 minutes
    recent = timezone.now() - datetime.timedelta(minutes=5)

    by_result = collections.defaultdict(list)
    for step in Step.objects.filter(result__test=test):
        by_result[step.result_id].append(step)

    machines = []
    for row in rows:
        name = (row.machine.info or {}).get('machine_name')
        machines.append({
            'name': name if name and name != 'None' else 'machine %d' % (row.machine.id),
            'machine_id': row.machine.id, 'user': row.machine.user.username,
            'games': row.games, 'crashes': row.crashes, 'timeloss': row.timeloss, 'updated_at': iso(row.updated),
            'active': row.machine.workload == test.id and row.machine.updated >= recent,
            'workloads': row.workloads,
        })
        steps = walk.walk_document(by_result[row.id], iso)
        if steps is not None:
            machines[-1]['steps'] = steps

    document = {
        'schema_version': 1,
        'match': {
            'id': match_id(test.id),
            'name': test.name or '%s vs %s' % (player_name(test.dev_engine, test.dev.name), test.base.name),
            'kind': 'sprt' if test.test_mode == 'SPRT' else 'games',
            'state': state,
            # as the fork sent it; there is no OpenBench page to link to, and a match of a test in the lab shows none
            'producer': { 'name': 'OpenBench', 'version': instance(), 'url': None },
            'started_at': iso(test.creation),
            'updated_at': iso(timezone.now()),
            'finished_at': finished_at,
            'host': { 'name': instance() },
            'settings': {
                'tc': test.dev_time_control, 'book': test.book_name,
                'runner': { 'name': test.runner }, 'pgn_options': PGN_OPTIONS.get(test.runner, []),
                # Fixed once nyayo has the match, so only what a test can't change: not its
                # workload size (MODIFY), upload_pgns or scale_nps (both set at a first workload)
                'openbench': {
                    'test_mode': test.test_mode, 'max_games': test.max_games,
                    'base_time_control': test.base_time_control,
                    'win_adj': test.win_adj, 'draw_adj': test.draw_adj,
                    'syzygy_wdl': test.syzygy_wdl, 'syzygy_adj': test.syzygy_adj,
                    'scale_method': test.scale_method,
                    'dev_network': test.dev_netname, 'base_network': test.base_netname,
                },
            },
        },
        'subject': {
            'name': test.dev_engine, 'version': '', 'commit': test.dev.sha, 'branch': test.dev.name,
            'repo': test.dev_repo, 'bench': test.dev.bench, 'options': uci_options(test.dev_options),
        },
        'opponents': [{
            'id': 'base', 'name': test.base_engine, 'version': '', 'commit': test.base.sha, 'branch': test.base.name,
            'repo': test.base_repo, 'bench': test.base.bench, 'options': uci_options(test.base_options),
            'rating': None,
        }],
        'results': [{
            'opponent': 'base',
            'games': count('games'), 'wins': count('wins'), 'draws': count('draws'), 'losses': count('losses'),
            'penta': [count('LL'), count('LD'), count('DD'), count('DW'), count('WW')],
            # OpenBench doesn't know which side crashed or lost on time
            'timelosses': { 'subject': count('timeloss'), 'opponent': 0 },
            'crashes': { 'subject': count('crashes'), 'opponent': 0 },
        }],
        'progress': None,
        'games': None,
        'sprt': sprt,
        'stats': stats,
        'machines': machines,
    }

    verdict = walk.verdict_document(test, state)
    if verdict is not None:
        document['match']['verdict'] = verdict
    return document


def report_test(test):
    """Builds a test's match document and ingests it, in this process. `test` is a Test or its id.

    Build and ingest run in one write transaction, so two reports of a test are serialised by the database and the
    later one is built from what the earlier left: a document never arrives after a newer one with fewer games. A
    failure is logged, never raised: a worker's report must not fail because a document did. Returns the Ingested
    (its `.match` and `.created`), or None on a failure."""
    # imported here: nyayo.matches reads models of this app's neighbours, and this module is imported by views
    from nyayo.matches.ingest import ingest

    try:
        with transaction.atomic():
            if not isinstance(test, Test):
                test = Test.objects.select_related('dev', 'base').get(id=test)
            document = build_document(test)
            return ingest(json.dumps(document).encode('utf-8'), None, document['match']['id'])
    except Exception:
        logger.exception('The match of test %s could not be reported', getattr(test, 'id', test))
        return None
