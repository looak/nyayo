# A tune's builds carry extra `make` words (`TUNE=1`): the client appends them, refuses ones that don't fit, and keeps the binary
# apart from a normal build of the same commit.
# Run with pytest, from anywhere, with the client's requirements installed (nyayo/client/requirements.txt).

import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, os.path.pardir, os.path.pardir, 'client')))


def test_the_words_end_the_make_command():
    import utils
    command = utils.makefile_command('', '.', 'Elephant-ABCD1234', 'g++', ['TUNE=1'])
    assert command == ['make', '-j4', 'EXE=Elephant-ABCD1234', 'CXX=g++', 'TUNE=1']


def test_no_words_is_the_command_it_always_was(tmp_path):
    import utils
    assert utils.makefile_command('', '.', 'E', 'g++') == ['make', '-j4', 'EXE=E', 'CXX=g++']
    net = tmp_path / 'net.nnue'
    command = utils.makefile_command(str(net), '.', 'E', None, [])
    assert command[:3] == ['make', '-j4', 'EXE=E'] and command[3].startswith('EVALFILE=') and len(command) == 4


@pytest.mark.parametrize('word', ['TUNE', '=1', 'TUNE=1;ls', 'A=$(x)', 'A=b&c', 'A=b c', 'A=`x`', '-j8', 'EXE=other', 'CXX=evil', 'EVALFILE=x', 5])
def test_a_word_that_does_not_fit_refuses_the_workload(word):
    import utils
    for call in (lambda: utils.checked_make_args(['TUNE=1', word]),
                 lambda: utils.makefile_command('', '.', 'E', 'g++', [word]),
                 lambda: utils.engine_binary_name('Elephant', 'a' * 40, '', [word])):
        with pytest.raises(utils.OpenBenchBuildFailedException):
            call()


def test_a_tune_build_never_shares_a_cached_binary_with_a_normal_one():
    import utils
    plain = utils.engine_binary_name('Elephant', 'a' * 40, '')
    tuned = utils.engine_binary_name('Elephant', 'a' * 40, '', ['TUNE=1'])
    assert plain == 'Elephant-AAAAAAAA' == utils.engine_binary_name('Elephant', 'a' * 40, '', [])
    assert tuned != plain and tuned.startswith(plain + '-')
    assert tuned == utils.engine_binary_name('Elephant', 'a' * 40, '', ['TUNE=1'])
    assert tuned != utils.engine_binary_name('Elephant', 'a' * 40, '', ['TUNE=2'])
    assert tuned != utils.engine_binary_name('Elephant', 'a' * 40, '', ['TUNE=1', 'X=1'])


def test_the_binary_is_looked_up_under_the_name_with_the_hash(tmp_path, monkeypatch):
    import utils
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(utils, 'IS_LINUX', True)
    monkeypatch.setattr(utils, 'IS_WINDOWS', False)
    plain = tmp_path / utils.engine_binary_name('Elephant', 'a' * 40, '')
    plain.write_text('normal build')
    tuned = str(tmp_path / utils.engine_binary_name('Elephant', 'a' * 40, '', ['TUNE=1']))
    assert utils.check_for_engine_binary(str(plain)) and not utils.check_for_engine_binary(tuned)


def test_the_worker_reads_the_words_from_the_workload_and_refuses_a_bad_one():
    import utils, worker
    config = types.SimpleNamespace(workload={ 'test' : { 'make_args' : ['TUNE=1'] } })
    assert worker.make_args_of(config) == ['TUNE=1']
    assert worker.make_args_of(types.SimpleNamespace(workload={ 'test' : {} })) == []
    with pytest.raises(utils.OpenBenchBuildFailedException):
        worker.make_args_of(types.SimpleNamespace(workload={ 'test' : { 'make_args' : ['A=b;c'] } }))
    with pytest.raises(utils.OpenBenchBuildFailedException):
        worker.complete_workload(types.SimpleNamespace(workload={ 'test' : { 'make_args' : ['A=b;c'] } }))


def test_prepare_engine_builds_with_the_words(tmp_path, monkeypatch):
    import io, utils, zipfile
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'Engines').mkdir()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z:
        z.writestr('root/ob_build/README', 'x')
    monkeypatch.setattr(utils.requests, 'get', lambda *a, **k: types.SimpleNamespace(content=buffer.getvalue()))
    seen = []

    class Process:
        returncode = 0
        def __init__(self, command, **kw):
            seen.append(command)
            open(os.path.join(kw['cwd'], command[2][4:]), 'w').write('built')
        def communicate(self):
            return (b'', None)

    monkeypatch.setattr(utils.subprocess, 'Popen', Process)
    out_path = os.path.join('Engines', utils.engine_binary_name('Elephant', 'a' * 40, '', ['TUNE=1']))
    utils.prepare_engine('Elephant', '', 'main', 'http://x', 'ob_build', out_path, False, 'g++', ['TUNE=1'])
    assert seen[0][-1] == 'TUNE=1' and utils.check_for_engine_binary(out_path)
