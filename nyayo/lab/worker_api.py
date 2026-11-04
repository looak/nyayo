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
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

# The worker's API: every client* endpoint of OpenBench's server, at the same path with the same answers, so OpenBench's
# Client (openbench_eg's Client/) runs against nyayo unchanged. Adapted from the client hook views of OpenBench's
# OpenBench/views.py (openbench_eg da629d7). What changed: a worker signs in as a Django user (an inactive one is
# refused; there is no Profile to enable), every report goes to nyayo's match in this process (document.report_test), a
# workload's upload becomes one of nyayo's batches, and the settings come from config.py, not Config/config.json.

import json
import logging
import os
import secrets

from django.conf import settings
from django.contrib.auth import authenticate
from django.db import IntegrityError, transaction
from django.db.models import F
from django.http import FileResponse, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from nyayo.lab import config, hours, rtt, steps, utils
from nyayo.lab.document import report_test
from nyayo.lab.get_workload import get_workload
from nyayo.lab.ids import match_id
from nyayo.lab.models import EngineConfig, LogEvent, Machine, Network, Result, ServerState, Test
from nyayo.runs.ingest import IngestError

logger = logging.getLogger(__name__)


def reported(test_id):
    # Tells nyayo of a test once everything that changed it is committed. A report that fails is logged, not raised.
    transaction.on_commit(lambda: report_test(test_id))


def authenticate_worker(request):
    # The user's credentials, as a worker sends them with its first request of a session. Django's own check, which
    # refuses an inactive user: that is how a worker's user is disabled.
    try:
        return authenticate(username=request.POST['username'], password=request.POST['password'])
    except KeyError:
        return None


def verify_worker(function):

    def wrapped_verify_worker(*args, **kwargs):

        # Get the machine, assuming it exists
        try: machine = Machine.objects.get(id=int(args[0].POST['machine_id']))
        except Exception: return JsonResponse({ 'error' : 'Bad Client Version: Bad Machine Id' })

        # Ensure the Client is using the same version as the Server
        if machine.info['client_ver'] != config.client_version():
            return JsonResponse({ 'error' : 'Bad Client Version: Expected %d' % (config.client_version()) })

        # Prompt the worker to soft-restart if its config is out of date
        if machine.info.get('OPENBENCH_CONFIG_CHECKSUM') != ServerState.checksum():
            return JsonResponse({ 'error' : 'Bad Client Version: Server Configuration Changed' })

        # Use the secret token as our soft verification
        if machine.secret != args[0].POST.get('secret'):
            return JsonResponse({ 'error' : 'Bad Client Version: Invalid Secret Token' })

        # Otherwise, carry on, and pass along the machine
        return function(*args, machine)

    wrapped_verify_worker.__name__ = function.__name__
    return wrapped_verify_worker


@csrf_exempt
def client_version_ref(request):
    # Enough information to download the right Client
    return JsonResponse({
        'client_version'  : config.client_version(),
        'client_repo_url' : config.client_repo_url(),
        'client_repo_ref' : config.client_repo_ref(),
    })

@csrf_exempt
def client_match_runner_version_ref(request):

    # Enough information to build the right Fastchess version
    return JsonResponse({
        'fastchess_min_version' : config.fastchess_min_version(),
        'fastchess_repo_url'    : config.fastchess_repo_url(),
        'fastchess_repo_ref'    : config.fastchess_repo_ref(),
    })

@csrf_exempt
def client_get_build_info(request):

    ## Information pulled from the config about how to build each engine.
    ## Toss in a private flag as well to indicate the need for Github Tokens.

    data = {}
    for engine in EngineConfig.objects.all():
        data[engine.name] = { **engine.build(), 'private' : engine.private }
    return JsonResponse(data)

@csrf_exempt
def client_worker_info(request):

    # Verify the User's credentials
    if (user := authenticate_worker(request)) is None:
        return JsonResponse({ 'error' : 'Bad Credentials' })

    # Request update before creating a machine
    info         = json.loads(request.POST['system_info'])
    expected_ver = config.client_version()

    if info.get('client_ver') != expected_ver:
        return JsonResponse({ 'error' : 'Bad Client Version: Expected %d' % (expected_ver)})

    # Create a new Machine for this session
    machine = Machine(user=user, info=info)

    # Save the machine's latest information and Secret Token for this session
    machine.info   = info
    machine.secret = secrets.token_hex(32)

    # Note the Config checksum at the time of init, in case it changes
    machine.info['OPENBENCH_CONFIG_CHECKSUM'] = ServerState.checksum()

    # Tag engines that the Machine can build and/or run with binaries
    machine.info['supported'] = []
    for engine in EngineConfig.objects.all():

        build = engine.build()

        # Must have all CPU flags, for both Public and Private engines
        if any([flag not in machine.info['cpu_flags'] for flag in build['cpuflags']]):
            continue

        # Private engines must have, or think they have, a Git Token
        if engine.private and engine.name not in machine.info['tokens'].keys():
            continue

        # Public engines must have a compiler of a sufficient version
        if not engine.private and engine.name not in machine.info['compilers'].keys():
            continue

        # Must match the Operating Systems supported by the engine
        if machine.info['os_name'] not in build['systems']:
            continue

        # All requirements are met, and this Machine can play with the given engine
        machine.info['supported'].append(engine.name)

    # Finish up
    machine.save()

    # Pass back the Machine Id, and Secret Token for this session
    return JsonResponse({ 'machine_id' : machine.id, 'secret' : machine.secret })

@csrf_exempt
def client_get_network(request, engine, name):

    # Verify the User's credentials
    if authenticate_worker(request) is None:
        return HttpResponse('Bad Credentials')

    # A network is found by its name or by its sha, for this engine, and kept as a file named by its sha
    candidates = Network.objects.filter(engine=engine)
    network    = candidates.filter(name=name).first() or candidates.filter(sha256=name).first()
    path       = os.path.join(settings.DATA_DIR, 'networks', network.sha256) if network else ''

    if not network or not os.path.isfile(path):
        return HttpResponse('No network found with matching Sha', status=404)

    response = FileResponse(open(path, 'rb'), content_type='application/octet-stream')
    response['Content-Disposition'] = 'attachment; filename=%s' % (os.path.basename(path))
    return response

@csrf_exempt
@verify_worker
def client_get_workload(request, machine):
    rtt.note(request, machine)
    hours.note(machine)
    try:
        return JsonResponse(get_workload(request, machine))
    except IntegrityError as error:
        return JsonResponse({ 'error' : f'IntegrityError: {error}' })

@csrf_exempt
@verify_worker
def client_bench_error(request, machine):

    # The test is not stopped here: the bench is the worker's step, and the verdict on it (clientReportStep)
    # says whether the test stops. This keeps the event, and the report.
    test = Test.objects.get(id=int(request.POST['test_id']))

    # Log the error into the Events table
    LogEvent.objects.create(
        author     = machine.user.username,
        summary    = request.POST['error'][:128],
        log_file   = '',
        machine_id = int(request.POST['machine_id']),
        test_id    = int(request.POST['test_id']))

    reported(test.id)

    return JsonResponse({})

@csrf_exempt
@verify_worker
def client_report_step(request, machine):

    # A step of the worker's walk, as it starts or ends. Best effort on both sides: a step that can't be
    # recorded is no reason to fail the worker's workload.
    try:
        test   = Test.objects.get(id=int(request.POST['test_id']))
        result = Result.objects.get(test=test, machine=machine)
        form   = request.POST.dict()
        step   = steps.record_step(result, form)

        # A stage that has ended on both engines is judged, and the worker answered { 'stop' } or { 'retry_after' }
        answer, judged = {}, False
        if form.get('state') in ('passed', 'failed') and form.get('step') in ('compile', 'bench'):
            answer, judged = steps.settle(result, form['step'], form.get('side', ''), getattr(step, 'was_passed', False))
    except (Test.DoesNotExist, Result.DoesNotExist, KeyError, ValueError):
        return JsonResponse({})

    # A verdict can stop the test or move another machine's: the report is built from what is committed
    if step is not None or judged:
        reported(test.id)

    return JsonResponse(answer)

@csrf_exempt
@verify_worker
def client_submit_nps(request, machine):

    # Update the NPS counters for the GUI views
    machine.mnps      = float(request.POST['nps'     ]) / 1e6
    machine.dev_mnps  = float(request.POST['dev_nps' ]) / 1e6
    machine.base_mnps = float(request.POST['base_nps']) / 1e6
    hours.note(machine)
    machine.save(update_fields=['mnps', 'dev_mnps', 'base_mnps', 'updated'])

    # Pass back an empty JSON response
    return JsonResponse({})

@csrf_exempt
@verify_worker
def client_submit_error(request, machine):

    # Report an error when working on test. This could be one three kinds.
    # 1. Error building the engine. Does not compile, for whatever reason.
    # 2. Error during actual gameplay. Timeloss, Disconnect, Crash, etc.

    # Log the Error into the Events table
    event = LogEvent.objects.create(
        author     = machine.user.username,
        summary    = request.POST['error'][:128],
        log_file   = '',
        machine_id = int(request.POST['machine_id']),
        test_id    = int(request.POST['test_id']))

    # Save the Logs to be viewed later
    os.makedirs(os.path.join(settings.DATA_DIR, 'logs'), exist_ok=True)
    event.log_file = 'event%d.log' % (event.id)
    with open(os.path.join(settings.DATA_DIR, 'logs', event.log_file), 'w', encoding='utf-8') as fout:
        fout.write(request.POST['logs'])
    event.save()

    return JsonResponse({})

@csrf_exempt
@verify_worker
def client_submit_results(request, machine):

    # Returns {}, or { 'stop' : True }
    return JsonResponse(utils.update_test(request, machine))

@csrf_exempt
@verify_worker
def client_heartbeat(request, machine):

    # Force a refresh of the updated timestamp
    rtt.note(request, machine)
    hours.note(machine)
    machine.save(update_fields=['updated', *rtt.changed(machine)])

    # Include a 'stop' header iff the test was finished
    finished = Test.objects.filter(id=int(request.POST['test_id'])).values_list('finished', flat=True).first()

    return JsonResponse([{}, { 'stop' : True }][bool(finished)])

@csrf_exempt
@verify_worker
def client_submit_nps_stats(request, _):

    result_id = int(request.POST['result_id'])

    # No risk from concurrent access
    Result.objects.filter(id=result_id).update(
        dev_nodes        = F('dev_nodes'       ) + int(request.POST['dev_nodes'       ]),
        dev_time         = F('dev_time'        ) + int(request.POST['dev_time'        ]),
        dev_time_scaled  = F('dev_time_scaled' ) + int(request.POST['dev_time_scaled' ]),
        base_nodes       = F('base_nodes'      ) + int(request.POST['base_nodes'      ]),
        base_time        = F('base_time'       ) + int(request.POST['base_time'       ]),
        base_time_scaled = F('base_time_scaled') + int(request.POST['base_time_scaled']),
        updated          = timezone.now(),
    )

    return JsonResponse({})

@csrf_exempt
@verify_worker
def client_submit_pgn(request, machine):

    # A workload's games, bz2 as the worker wrote them, named and tagged already (Client/pgn_util.py). They become one
    # of nyayo's uploads of finished games (docs/match-schema.md, Batches), named <result>.<book index> as the fork's
    # reporter named them; the same bytes sent again are recognised. Nothing is kept as OpenBench kept it.
    from nyayo.matches import uploads

    test_id = int(request.POST['test_id'])
    name    = '%d.%d' % (int(request.POST['result_id']), int(request.POST['book_index']))
    data    = request.FILES['file'].read()

    # A workload that played nothing sends an empty archive (14 bytes), which nyayo would refuse for holding no game
    if len(data) < 64 and not uploads.bunzip(data, 64).strip():
        return JsonResponse({})

    try:
        # nyayo must know the match before it takes an upload of its games
        if report_test(test_id) is None:
            raise IngestError(500, 'the match of test %d could not be reported' % (test_id))
        uploads.store(match_id(test_id), name, data, uploads.BZIP2, None, None)
    except IngestError as error:
        logger.error('nyayo refused upload %s of test %d: %d %s', name, test_id, error.status, error.reason)
    except Exception:
        logger.exception('upload %s of test %d could not be stored', name, test_id)

    return JsonResponse({})
