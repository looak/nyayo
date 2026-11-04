
# Adapted for nyayo from OpenBench's OpenBench/workloads/verify_workload.py (openbench_eg da629d7): what is asked of
# GitHub for a branch or a commit. requests is urllib here, and the token comes from NYAYO_GITHUB_TOKEN.

import json
import os
import re
import urllib.error
import urllib.request

from . import utils

TOKEN_VARIABLE = 'NYAYO_GITHUB_TOKEN'

class GithubDown(Exception):
    """GitHub did not answer: no connection, a timeout, or a server error. Not the same as GitHub saying no."""

def api_get(url, timeout=10):

    # (status, parsed json); a 404 is an answer, a failure to get one is GithubDown. Tests replace this function.
    headers = { 'Accept' : 'application/vnd.github+json', 'User-Agent' : 'nyayo' }
    if token := os.environ.get(TOKEN_VARIABLE):
        headers['Authorization'] = 'token %s' % token.strip()

    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
            return response.status, json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        if error.code >= 500:
            raise GithubDown('GitHub answered %d' % error.code)
        try: return error.code, json.loads(error.read().decode('utf-8'))
        except Exception: return error.code, {}
    except Exception as error:
        raise GithubDown(str(error))

def api_base(repo):
    # NYAYO_GITHUB_API stands in for https://api.github.com/repos, for the smoke test's own GitHub
    root = os.environ.get('NYAYO_GITHUB_API', '').strip().rstrip('/') or 'https://api.github.com/repos'
    return re.sub(r'^https://github\.com', root, repo.strip())

def is_sha(ref):
    return bool(re.search('^[0-9a-fA-F]{40}$', ref))

def look_up(repo, ref):

    # What GitHub says of a branch, a tag or a sha: { sha, message }, or None when it isn't there. Raises GithubDown.
    base  = api_base(repo)
    bysha = is_sha(ref)

    # Lookup branch or commit sha, but will fail for tags
    status, data = api_get(utils.path_join(base, 'commits' if bysha else 'branches', ref))

    # Check to see if the branch name was actually a tag name
    if not bysha and 'commit' not in data:
        status, data = api_get(utils.path_join(base, 'commits', ref))

    # Actual branches have to go one layer deeper
    elif not bysha: data = data['commit']

    if not isinstance(data, dict) or 'sha' not in data or 'message' not in (data.get('commit') or {}):
        return None

    return { 'sha' : data['sha'], 'message' : data['commit']['message'] }

def branch_of_sha(repo, sha):

    # The branch a commit is the head of, preferring one that is not the default, or else the head branch of a pull
    # request with the commit in it ( for a commit whose branch has moved on or is gone ). Whatever goes wrong,
    # None: a lookup never stops a test from being made
    base = api_base(repo)

    try:
        names = [x['name'] for x in api_get(utils.path_join(base, 'commits', sha, 'branches-where-head'), 10)[1]]
        if len(names) > 1:
            default = api_get(base, 10)[1].get('default_branch')
            names   = [x for x in names if x != default] or names
        if names: return names[0]
    except Exception: pass

    try:
        for pull in api_get(utils.path_join(base, 'commits', sha, 'pulls'), 10)[1]:
            head, into = pull['head'], pull['base']
            same = (head.get('repo') or {}).get('full_name') == into['repo']['full_name']
            if same and head['ref']: return head['ref']
    except Exception: pass

    return None

def commits_behind(repo, dev_sha, base_sha):

    # How many commits the base has that the dev branch has not, or None when GitHub can't say
    if dev_sha == base_sha:
        return 0

    try:
        status, data = api_get(utils.path_join(api_base(repo), 'compare', '%s...%s' % (dev_sha, base_sha)))
        return int(data['ahead_by']) if 'ahead_by' in data else None
    except Exception:
        return None
