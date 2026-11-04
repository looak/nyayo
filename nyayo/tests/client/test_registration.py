#!/bin/python3

# The worker's part of registration without passwords (ramani plans/nyayo-2.md, Worker registration): hello and
# the token kept beside the client, the describe job and "wait", a client update that keeps the token, and fastchess
# as a binary from the server, and a client that comes from the server it works for. Run with pytest, from anywhere, with the
# client's requirements installed (nyayo/client/requirements.txt).

import hashlib
import io
import os
import stat
import sys
import tarfile
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT = os.path.abspath(os.path.join(HERE, os.path.pardir, os.path.pardir, 'client'))
sys.path.insert(0, CLIENT)


class Reply:
    def __init__(self, body=None, content=b'', status=200):
        self.body, self.content, self.status_code = body or {}, content, status
    def json(self): return self.body


@pytest.fixture
def token_file(monkeypatch, tmp_path):
    import client
    monkeypatch.setattr(client, 'TOKEN_FILE', str(tmp_path / 'worker.token'))
    return tmp_path / 'worker.token'


def config(**fields):
    base = dict(server='http://x', identity='alex-pc', join=None, token=None, toolchains={}, last_wait=None,
                machine_id=None, secret_token=None, cxx_comp='g++', blacklist={})
    return types.SimpleNamespace(**{ **base, **fields })


def test_hello_keeps_the_token_beside_the_client(monkeypatch, token_file):
    import worker

    sent = {}
    monkeypatch.setattr(worker.requests, 'post', lambda target, **kw: sent.update(target=target, **kw) or Reply({ 'token' : 'abc' }))

    cfg = config(join='ONE-TIME')
    worker.server_hello(cfg)

    assert sent['target'].endswith('clientHello/')
    assert sent['data']['name'] == 'alex-pc' and sent['data']['join'] == 'ONE-TIME'
    assert sent['data']['client_ver'] == worker.CLIENT_VERSION
    assert 'username' not in sent['data'] and 'password' not in sent['data'] and 'token' not in sent['data']
    assert cfg.token == 'abc' and token_file.read_text() == 'abc'
    if os.name != 'nt':
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    # A restart reads it again, and sends it along
    import client
    cfg = config(token=client.read_token())
    worker.server_hello(cfg)
    assert sent['data']['token'] == 'abc'


def test_a_refused_hello_ends_the_worker(monkeypatch, token_file):
    import worker

    monkeypatch.setattr(worker.requests, 'post', lambda *a, **kw: Reply({ 'error' : 'Bad join code' }))
    with pytest.raises(SystemExit):
        worker.server_hello(config(join='WRONG'))
    assert not token_file.exists()


def test_every_call_carries_the_token(monkeypatch):
    import worker

    sent = {}
    monkeypatch.setattr(worker.requests, 'post', lambda target, **kw: sent.update(kw) or Reply({}))
    worker.ServerReporter.report(config(token='abc'), 'clientHeartbeat', {})
    assert sent['data']['token'] == 'abc'


def test_the_describe_job_reports_the_tools_and_asks_again(monkeypatch, capsys):
    import worker

    versions = { 'g++' : '13.2.0', 'make' : '4.3' }
    def get_version(tool):
        if tool in versions: return versions[tool]
        raise Exception('missing')
    registered = []
    monkeypatch.setattr(worker, 'get_version', get_version)
    monkeypatch.setattr(worker, 'server_register_system', lambda cfg: registered.append(dict(cfg.toolchains)))
    monkeypatch.setattr(worker.requests, 'post', lambda *a, **kw: Reply({ 'describe' : ['g++', 'make', 'dotnet'] }))

    cfg = config(workload='left over')
    worker.server_request_workload(cfg)

    assert registered == [{ 'g++' : '13.2.0', 'make' : '4.3', 'dotnet' : None }]
    assert cfg.workload is None


def test_wait_prints_its_reason_once_and_polls_on(monkeypatch, capsys):
    import worker

    monkeypatch.setattr(worker.requests, 'post', lambda *a, **kw: Reply({ 'wait' : 'waiting for approval' }))
    cfg = config(workload='left over')
    for _ in range(3):
        worker.server_request_workload(cfg)

    assert cfg.workload is None
    assert capsys.readouterr().out.count('waiting for approval') == 1


def test_a_client_update_keeps_the_token(tmp_path):
    import client

    extracted, installed = tmp_path / 'Client', tmp_path / 'installed'
    extracted.mkdir(); installed.mkdir()
    (extracted / 'client.py').write_text('new client')
    (extracted / 'worker.py').write_text('new worker')
    (extracted / 'worker.token').write_text('from the archive')
    (installed / 'client.py').write_text('running client')
    (installed / 'worker.py').write_text('old worker')
    (installed / 'worker.token').write_text('mine')

    assert client.copy_client_files(str(extracted), str(installed)) == 1
    assert (installed / 'worker.token').read_text() == 'mine'
    assert (installed / 'client.py').read_text() == 'running client'
    assert (installed / 'worker.py').read_text() == 'new worker'


def make_zip(files, top=''):
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, content in files.items():
            archive.writestr(top + name, content)
    return buffer.getvalue()


def serve(monkeypatch, client, body, answer):
    # clientVersionRef answers `answer`, and the zip is `body`; what was asked is returned
    asked = []

    def post(target, **kw):
        asked.append(('POST', target))
        return Reply(answer)

    def get(target, **kw):
        asked.append(('GET', target))
        return Reply(content=body)

    monkeypatch.setattr(client.requests, 'post', post)
    monkeypatch.setattr(client.requests, 'get', get)
    return asked


@pytest.mark.parametrize('top', ['', 'nyayo-client/', 'Client/'])
def test_the_client_is_downloaded_from_the_server_and_keeps_the_token(monkeypatch, tmp_path, top):
    import client
    body = make_zip({'client.py': 'new client', 'worker.py': 'new worker', 'utils.py': 'new utils', 'worker.token': 'from the archive'}, top)
    asked = serve(monkeypatch, client, body, {'client_version': 55, 'client_url': '/client.zip', 'client_sha256': hashlib.sha256(body).hexdigest()})
    (tmp_path / 'client.py').write_text('running client')
    (tmp_path / 'worker.py').write_text('old worker')
    (tmp_path / 'worker.token').write_text('mine')
    monkeypatch.chdir(tmp_path)

    client.download_client_files(types.SimpleNamespace(server='http://nyayo:8001'))

    assert asked == [('POST', 'http://nyayo:8001/clientVersionRef/'), ('GET', 'http://nyayo:8001/client.zip/')] or \
           asked[1][1].startswith('http://nyayo:8001/client.zip')
    assert (tmp_path / 'worker.py').read_text() == 'new worker' and (tmp_path / 'utils.py').read_text() == 'new utils'
    assert (tmp_path / 'client.py').read_text() == 'running client'
    assert (tmp_path / 'worker.token').read_text() == 'mine'


def test_an_absolute_url_is_used_as_it_is_and_no_github_is_asked(monkeypatch, tmp_path):
    import client
    body = make_zip({'worker.py': 'w'})
    asked = serve(monkeypatch, client, body, {'client_url': 'http://elsewhere:9/client.zip'})
    monkeypatch.chdir(tmp_path)
    client.download_client_files(types.SimpleNamespace(server='http://nyayo:8001'))
    assert asked[1] == ('GET', 'http://elsewhere:9/client.zip')
    assert all('github' not in target for _, target in asked)


def test_a_client_that_does_not_match_the_checksum_is_not_installed(monkeypatch, tmp_path):
    import client
    serve(monkeypatch, client, make_zip({'worker.py': 'w'}), {'client_url': '/client.zip', 'client_sha256': '0' * 64})
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Exception, match='Unable to retrieve'):
        client.download_client_files(types.SimpleNamespace(server='http://nyayo:8001'))
    assert not (tmp_path / 'worker.py').exists()


def test_a_server_with_no_client_to_give_is_an_error_not_a_crash(monkeypatch, tmp_path):
    import client
    serve(monkeypatch, client, b'', {'error': 'nope'})
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Exception, match='Unable to retrieve'):
        client.download_client_files(types.SimpleNamespace(server='http://nyayo:8001'))


def test_an_updated_file_is_new_to_python_whatever_time_the_archive_gave_it(tmp_path):
    # compiled files are kept by the time and size of their source: 54 and 55 are the same size
    import client
    extracted, installed = tmp_path / 'new', tmp_path / 'installed'
    extracted.mkdir(); installed.mkdir()
    (extracted / 'worker.py').write_text('CLIENT_VERSION = 55')
    (installed / 'worker.py').write_text('CLIENT_VERSION = 54')
    stamp = 1767225600     # the archive's, 2026-01-01
    os.utime(extracted / 'worker.py', (stamp, stamp))
    os.utime(installed / 'worker.py', (stamp, stamp))

    client.copy_client_files(str(extracted), str(installed))

    assert (installed / 'worker.py').read_text() == 'CLIENT_VERSION = 55'
    assert os.stat(installed / 'worker.py').st_mtime > stamp + 86400


def test_the_client_takes_no_credentials(monkeypatch):
    import client

    monkeypatch.setattr(sys, 'argv', ['client.py', '-S', 'http://x', '--join', 'CODE', '-T', '4'])
    args = client.parse_arguments()
    assert args.server == 'http://x' and args.join == 'CODE'
    assert not hasattr(args, 'username') and not hasattr(args, 'password')


def test_a_missing_compiler_is_reported_not_fatal(monkeypatch, capsys):
    import worker

    monkeypatch.setattr(worker, 'locate_utility', lambda *a, **kw: None)
    cfg = types.SimpleNamespace()
    worker.Configuration.check_requirements(cfg)
    assert cfg.cxx_comp is None
    assert 'missing' in capsys.readouterr().out


def runner_data(content, url='fastchess-linux-x86-64.tar', sha=None):
    return { 'binary' : { 'url' : url, 'sha256' : sha or hashlib.sha256(content).hexdigest() }, 'fastchess_min_version' : '1.0.0' }


def tar_with(name, content):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        info = tarfile.TarInfo('fastchess-linux-x86-64/' + name)
        info.size, info.mode = len(content), 0o755
        archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


@pytest.mark.skipif(os.name == 'nt', reason='the stand-in runner is a shell script')
def test_a_downloaded_fastchess_is_checked_and_used(monkeypatch, tmp_path):
    import worker

    monkeypatch.chdir(tmp_path)
    script  = b'#!/bin/sh\necho "fastchess alpha 1.8.2"\n'
    archive = tar_with('fastchess', script)
    monkeypatch.setattr(worker.requests, 'get', lambda url, **kw: Reply(content=archive))

    cfg = config()
    assert worker.download_match_runner_binary(cfg, 'fastchess', runner_data(archive)) is True
    assert cfg.fastchess_ver == '1.8.2'
    assert os.access(tmp_path / 'fastchess-ob', os.X_OK)

    # A checksum that fails leaves what was there alone and says so
    (tmp_path / 'fastchess-ob').write_text('kept')
    assert worker.download_match_runner_binary(cfg, 'fastchess', runner_data(archive, sha='0' * 64)) is False
    assert (tmp_path / 'fastchess-ob').read_text() == 'kept'
    assert not (tmp_path / 'fastchess-ob.part').exists()

    # A binary that won't run is no binary
    broken = tar_with('fastchess', b'not a program')
    monkeypatch.setattr(worker.requests, 'get', lambda url, **kw: Reply(content=broken))
    assert worker.download_match_runner_binary(cfg, 'fastchess', runner_data(broken)) is False


def test_without_a_binary_or_a_compiler_there_is_nothing_to_do(monkeypatch, tmp_path):
    import worker, utils

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(worker.requests, 'post', lambda *a, **kw: Reply({ 'fastchess_min_version' : '1.0.0' }))
    monkeypatch.setattr(worker, 'locate_utility', lambda *a, **kw: None)

    with pytest.raises(utils.OpenBenchMatchRunnerBuildFailedException):
        worker.server_configure_match_runner(config(cxx_comp=None), 'fastchess', None)
