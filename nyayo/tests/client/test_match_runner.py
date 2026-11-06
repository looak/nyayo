#!/bin/python3

# The client's match runner: started by its full path (not its bare name, which Windows with
# NoDefaultCurrentDirectoryInExePath does not look for in the current folder), and a runner that could not start,
# died, or ended with no finished game is a failure to report as the failed `running` step.
# Run with pytest, from anywhere, with the client's requirements installed (nyayo/client/requirements.txt).

import io
import os
import shlex
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT = os.path.abspath(os.path.join(HERE, os.path.pardir, os.path.pardir, 'client'))
sys.path.insert(0, CLIENT)

import pytest


def finished(number, result='1-0'):
    return 'Finished game %d (a-dev vs a-base): %s {White mates}' % (number, result)


class FakePopen:
    # A match runner that says these lines and exits with this code
    def __init__(self, lines, code=0):
        self.stdout = io.BytesIO(b''.join(line.encode() + b'\n' for line in lines))
        self.code = code

    def poll(self):
        return self.code


def run(worker, monkeypatch, popen, abort=None):
    monkeypatch.setattr(worker, 'Popen', popen)
    abort = abort or threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        tasks = [executor.submit(worker.run_and_parse_runner, None, 'fastchess-ob', 0, worker.multiprocessing.Queue(), abort)]
    return tasks, abort


def test_the_match_runner_is_started_by_its_full_path_where_the_current_folder_is_not_searched(tmp_path, monkeypatch):
    import worker

    monkeypatch.setenv('NoDefaultCurrentDirectoryInExePath', '1')
    monkeypatch.chdir(tmp_path)
    command = shlex.split(worker.MatchRunner.executable(None) + ' -concurrency 2')

    assert os.path.isabs(command[0]) and os.path.dirname(command[0]) == str(tmp_path)
    assert os.path.basename(command[0]) == ['fastchess-ob.exe', 'fastchess-ob'][worker.IS_LINUX]
    assert command[1:] == ['-concurrency', '2']


def test_a_path_with_spaces_and_backslashes_survives_the_command_line(tmp_path, monkeypatch):
    import worker

    folder = tmp_path / 'my worker dir'
    folder.mkdir()
    monkeypatch.chdir(folder)
    command = worker.MatchRunner.executable(None) + ' -rounds 1'
    assert shlex.split(command)[0] == worker.MatchRunner.executable_path()
    assert shlex.split(command)[1:] == ['-rounds', '1']


def test_the_bench_and_genfens_start_the_engine_by_its_full_path(tmp_path, monkeypatch):
    import bench
    import genfens

    monkeypatch.chdir(tmp_path)
    seen = []
    monkeypatch.setattr(bench.subprocess, 'Popen', lambda cmd, **kw: seen.append(cmd) or (_ for _ in ()).throw(OSError()))
    bench.single_core_bench('engine-dev', types.SimpleNamespace(put=lambda x: None))
    assert os.path.isabs(seen[0][0]) and seen[0][1:] == ['bench']
    assert os.path.isabs(genfens.genfens_command_builder({ 'engine' : 'engine-dev', 'N' : 1, 'seeds' : [1], 'book' : 'b', 'extra' : '' }, 0)[0])


def test_a_runner_that_finished_games_is_no_failure(monkeypatch):
    import worker

    tasks, abort = run(worker, monkeypatch, lambda command, **kw: FakePopen(['fastchess 1.0', '', finished(1), finished(2, '0-1')]))
    assert tasks[0].result()['games'] == 2  # a blank line is not the end of its output
    assert worker.runner_failure(tasks, abort) is None


def test_a_runner_that_ended_with_no_finished_game_says_what_it_said(monkeypatch):
    import worker

    tasks, abort = run(worker, monkeypatch, lambda command, **kw: FakePopen(['Error: no engine', 'bye'], code=3))
    message, log = worker.runner_failure(tasks, abort)
    assert message == 'Match runner #0 ended with no finished games (exit code 3)'
    assert log == 'Error: no engine\nbye'


def test_a_runner_that_could_not_be_started_is_a_failure_with_its_error(monkeypatch):
    import worker

    def popen(command, **kwargs):
        raise FileNotFoundError(2, 'The system cannot find the file specified')

    tasks, abort = run(worker, monkeypatch, popen)
    message, log = worker.runner_failure(tasks, abort)
    assert message.startswith('Match runner #0 could not be run: ') and 'cannot find the file' in message
    assert 'FileNotFoundError' in log


def test_a_test_that_was_stopped_is_not_a_failure_of_its_runner(monkeypatch):
    import worker

    abort = threading.Event()
    abort.set()
    tasks, abort = run(worker, monkeypatch, lambda command, **kw: FakePopen([]), abort)
    assert worker.runner_failure(tasks, abort) is None


def test_the_failed_step_is_reported_with_its_message_and_the_worker_leaves_the_test(monkeypatch):
    import worker

    sent = []
    monkeypatch.setattr(worker.ServerReporter, 'report_step', lambda config, step, side, state, **fields: sent.append((step, side, state, fields)))
    with pytest.raises(worker.utils.OpenBenchRunnerFailedException) as raised:
        worker.fail_running(None, 'Match runner #0 ended with no finished games', 'the log', worker.time.time() - 2)
    assert str(raised.value) == 'Match runner #0 ended with no finished games'
    (step, side, state, fields), = sent
    assert (step, side, state) == ('running', None, 'failed')
    assert fields['detail'] == 'Match runner #0 ended with no finished games' and fields['log'] == 'the log'
    assert fields['seconds'] >= 2
