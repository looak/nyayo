#!/bin/python3

# The fork's workers upload each game with its final players and tags (nyayo's game database), on fastchess's games
# (example_fastchess.pgn, from the smoke test):
#   - pgn_util names <engine>-dev and <engine>-base <engine> <branch>, tags Subject and Opponent, keeps Termination
#   - every move keeps fastchess's whole comment (nyayo's game page reads the time, nodes and the end from it)
#   - fastchess still plays under <engine>-dev and <engine>-base, so the result counts are unchanged
# Run with pytest, from anywhere, with the client's requirements installed (nyayo/client/requirements.txt).

import importlib.util
import os
import re
import sys
import types

# The client's own files, which are modules of their own
HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT = os.path.abspath(os.path.join(HERE, os.path.pardir, os.path.pardir, 'client'))
sys.path.insert(0, CLIENT)

import pgn_util

PLAYERS = { 'dev' : 'smoke feature "x"', 'base' : 'smoke master' }
EXAMPLE = os.path.join(HERE, 'example_fastchess.pgn')

def tags_of(text):
    return dict(re.findall(r'^\[(\w+) "([^"]*)"\]$', text, re.M))

def comments_of(text):
    return re.findall(r'\{([^}]*)\}', text)

def test_names_and_tags():

    games = list(pgn_util.pgn_iterator(EXAMPLE))
    assert [h['White'] for h, _ in games[:2]] == ['smoke-dev', 'smoke-base']

    for headers, _ in games:
        dev_white = headers['White'].endswith('-dev')
        for compact in (True, False):

            tags = tags_of(pgn_util.format_headers(dict(headers), compact, PLAYERS))
            dev, base = "smoke feature 'x'", 'smoke master'  # A quote in a name becomes ', as the reporter does
            assert (tags['White'], tags['Black']) == ((dev, base) if dev_white else (base, dev)), tags
            assert tags['Subject'] == ('white' if dev_white else 'black'), tags
            assert tags['Opponent'] == 'base', tags
            assert tags['Termination'] == headers['Termination'] == 'adjudication', tags
            assert ('GameEndTime' in tags) == (not compact), tags

            # Without players: the runner's names, none of the fork's tags, Termination kept
            plain = tags_of(pgn_util.format_headers(dict(headers), compact))
            assert plain['White'] == headers['White'] and 'Subject' not in plain and 'Opponent' not in plain
            assert plain['Termination'] == 'adjudication'

    # Subject and Opponent come last, after every tag upstream keeps
    order = [x.split()[0][1:] for x in pgn_util.format_headers(dict(games[0][0]), False, PLAYERS).split('\n')]
    assert order[-2:] == ['Subject', 'Opponent'], order

def test_every_move_keeps_its_whole_comment():

    original = open(EXAMPLE).read()
    want     = comments_of(original)
    assert len(want) == sum(int(x) for x in re.findall(r'^\[PlyCount "(\d+)"\]', original, re.M))  # Every move has one
    assert all(re.match(r'^[+-]?M?\d+(\.\d+)?/\d+ \d+\.\d+s, n=\d+, sd=\d+', x) for x in want), want[:3]
    assert any('wins by adjudication' in x for x in want)  # The end is the last move's, after the nodes

    for compact in (True, False):
        for players in (None, PLAYERS):
            text = pgn_util.process_pgn_file(EXAMPLE, 1.0, compact, players)
            assert comments_of(text) == want

    # A book move keeps its word, a move with no comment says it is unknown, and the result ends the movetext
    comment = '+0.20/12 0.153s, tl=1.847s, n=120000, sd=14, nps=0, pv="g1f3"'
    out = pgn_util.format_movelist('1. e4 {book} e5 {%s} 2. Nf3 1-0' % (comment), True)
    assert out == 'e4 {book} e5 {%s} Nf3 {unknown} 1-0' % (comment), out

def test_counts_unchanged():

    # fastchess is still given <engine>-<side>: update_results reads its lines by word position
    sys.argv = sys.argv[:1]
    import worker

    workload = { 'test' : { 'type' : 'GAMES', 'syzygy_wdl' : 'DISABLED' } }
    for side in ('dev', 'base'):
        workload['test'][side] = {
            'options' : 'Threads=1 Hash=16', 'network' : None, 'private' : False,
            'engine' : 'smoke', 'name' : 'feature x', 'time_control' : 'N=1000',
        }
    config = types.SimpleNamespace(workload=workload)
    for side in ('dev', 'base'):
        settings = worker.MatchRunner.engine_settings(config, 'smoke-%s' % (side), side, 1.0, 0)
        assert settings.endswith(' name=smoke-%s' % (side)), settings

    lines = [
        'Finished game 1 (smoke-dev vs smoke-base): 1-0 {White mates}',
        'Finished game 2 (smoke-base vs smoke-dev): 1/2-1/2 {Draw by adjudication}',
        'Finished game 3 (smoke-dev vs smoke-base): 0-1 {White loses on time}',
        'Finished game 4 (smoke-base vs smoke-dev): 0-1 {Black mates}',
    ]
    results = { 'games' : {}, 'trinomial' : [0, 0, 0], 'pentanomial' : [0] * 5,
                'crashes' : 0, 'timelosses' : 0, 'illegals' : 0 }
    for line in lines:
        worker.MatchRunner.update_results(results, line)

    assert results['trinomial'] == [1, 1, 2], results     # L, D, W from dev's side
    assert results['pentanomial'] == [0, 0, 1, 1, 0], results
    assert results['timelosses'] == 1 and results['crashes'] == 0, results

def test_nps_is_counted_on_the_runners_names():

    # collect_nps_stats runs on fastchess's own file, before any renaming
    stats = pgn_util.collect_nps_stats([EXAMPLE], 1.0)
    assert stats['dev']['nodes'] > 0 and stats['base']['nodes'] > 0, stats
