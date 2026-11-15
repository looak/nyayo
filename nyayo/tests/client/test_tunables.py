# The worker reads an engine's tunable parameters over UCI: `spsa` if it answers with lines, else the spin options of `uci`.
# Run with pytest, from anywhere. The engines here are small scripts that behave like one.

import os
import stat
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, os.path.pardir, os.path.pardir, 'client')))

import tunables

SPSA = '''
Skill, int, 30, 0, 100, 5, 0.002
rfp_params__margin, int, 80, 0, 300, 15, 0.002
lmr_base, float, 0.75, 0.0, 2.0, 0.1, 0.002
'''.strip().splitlines()

UCI = [
    'id name toy',
    'option name Threads type spin default 1 min 1 max 1024',
    'option name Hash type spin default 16 min 1 max 65536',
    'option name Move Overhead type spin default 10 min 0 max 5000',
    'option name Skill type spin default 30 min 0 max 100',
    'option name Two Words type spin default 5 min 0 max 10',
    'option name Style type combo default Solid var Solid var Risky',
    'option name Clear Hash type button',
    'option name Margin type spin default 7 min 7 max 7',
    'option name Level type spin default 3 min 1 max 2',
    'option name Depth type spin default 4 min 0 max 6',
    'uciok',
]


def test_the_spsa_lines_are_used_as_they_are():
    assert tunables.parse_spsa(['noise'] + SPSA + ['info string done']) == [
        'Skill, int, 30, 0, 100, 5, 0.002', 'rfp_params__margin, int, 80, 0, 300, 15, 0.002', 'lmr_base, float, 0.75, 0.0, 2.0, 0.1, 0.002']


def test_lines_that_are_not_seven_fields_are_not_parameters():
    assert tunables.parse_spsa(['a, int, 1, 0, 2, 1', 'a, long, 1, 0, 2, 1, 0.1', 'a, int, x, 0, 2, 1, 0.1', 'option name x type spin']) == []


def test_a_name_twice_is_one_parameter():
    assert tunables.parse_spsa(['a, int, 1, 0, 2, 1, 0.1', 'a, int, 2, 0, 5, 1, 0.1']) == ['a, int, 1, 0, 2, 1, 0.1']


def test_uci_gives_the_spin_options_that_are_not_ordinary_with_a_guessed_step():
    found = tunables.parse_uci(UCI, ['Threads', 'Hash', 'Move Overhead'])
    assert found == ['Skill, int, 30, 0, 100, 5, 0.002', 'Depth, int, 4, 0, 6, 1, 0.002']


def test_the_step_is_a_twentieth_of_the_range_and_at_least_one():
    out = tunables.parse_uci(['option name Big type spin default 0 min -1000 max 1000', 'option name Small type spin default 1 min 0 max 5'])
    assert out == ['Big, int, 0, -1000, 1000, 100, 0.002', 'Small, int, 1, 0, 5, 1, 0.002']


def test_ordinary_names_match_without_case():
    assert tunables.parse_uci(['option name threads type spin default 1 min 1 max 8'], ['Threads']) == []


def engine(tmp_path, body):
    path = tmp_path / 'engine.py'
    path.write_text('#!%s\nimport sys\n%s\n' % (sys.executable, body))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    if os.name == 'nt':
        pytest.skip('a script is not an executable here')
    return str(path)


LOOP = '''
for line in sys.stdin:
    word = line.split()[0] if line.split() else ''
    if word == 'quit': break
%s
'''


def test_an_engine_that_answers_spsa_is_read_from_it(tmp_path):
    body = LOOP % '''
    if word == 'spsa':
        print("Skill, int, 30, 0, 100, 5, 0.002", flush=True)
    elif word == 'isready':
        print('readyok', flush=True)
    elif word == 'uci':
        print('option name Wrong type spin default 1 min 0 max 9'); print('uciok', flush=True)
'''
    assert tunables.read(engine(tmp_path, body), ['Threads'], wait=5) == ('spsa', ['Skill, int, 30, 0, 100, 5, 0.002'])


def test_an_engine_that_does_not_know_spsa_is_asked_uci(tmp_path):
    body = LOOP % '''
    if word == 'isready':
        print('readyok', flush=True)
    elif word == 'uci':
        print('option name Threads type spin default 1 min 1 max 8')
        print('option name Skill type spin default 30 min 0 max 100')
        print('uciok', flush=True)
    else:
        print('Unknown command: ' + word, flush=True)
'''
    assert tunables.read(engine(tmp_path, body), ['Threads'], wait=5) == ('uci', ['Skill, int, 30, 0, 100, 5, 0.002'])


def test_an_engine_that_dies_on_spsa_is_still_asked_uci(tmp_path):
    body = '''
for line in sys.stdin:
    word = line.split()[0] if line.split() else ''
    if word == 'spsa': sys.exit(3)
    if word == 'uci':
        print('option name Skill type spin default 30 min 0 max 100'); print('uciok', flush=True)
    if word == 'quit': break
'''
    assert tunables.read(engine(tmp_path, body), [], wait=5)[0] == 'uci'


def test_an_engine_with_nothing_to_tune_fails_with_what_it_said(tmp_path):
    body = LOOP % '''
    if word == 'isready':
        print('readyok', flush=True)
    elif word == 'uci':
        print('id name nothing'); print('option name Threads type spin default 1 min 1 max 8'); print('uciok', flush=True)
'''
    with pytest.raises(tunables.TunablesError) as error:
        tunables.read(engine(tmp_path, body), ['Threads'], wait=5)
    assert 'no spin option' in str(error.value) and 'id name nothing' in str(error.value)


def test_an_engine_that_says_nothing_fails_at_the_timeout(tmp_path):
    with pytest.raises(tunables.TunablesError) as error:
        tunables.read(engine(tmp_path, 'import time\ntime.sleep(30)'), [], wait=0.5)
    assert 'neither' in str(error.value)
