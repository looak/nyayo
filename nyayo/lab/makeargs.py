"""Extra `make` arguments of a build: `NAME=value` words, such as `TUNE=1`.

An engine that only exposes its tunable options in a special build says how to make it in its EngineConfig
(`tune_make_args`); a tune, its outcome checks and the SPRT queued after it carry the words on the Test (`make_args`) and
the worker passes them to `make`. The client checks the words again (`nyayo/client/utils.py`, with the same pattern),
because it runs what it is told.
"""

import re

from django.core.exceptions import ValidationError

WORD = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=[^\s;&|`$<>]*')

# What the worker sets itself: a word of these would move the binary or change the compiler it chose
RESERVED = ('EXE', 'CC', 'CXX', 'EVALFILE', 'MAKEFLAGS', 'SHELL')


def problem(text):
    """What is wrong with a string of words, or None."""
    for word in (text or '').split():
        if not WORD.fullmatch(word):
            return '"%s" is not NAME=value, with no spaces or shell characters' % word
        if word.partition('=')[0] in RESERVED:
            return '%s is set by the worker' % word.partition('=')[0]
    return None


def validate(text):
    """A field validator."""
    if (said := problem(text)) is not None:
        raise ValidationError(said)


def words(text):
    return (text or '').split()
