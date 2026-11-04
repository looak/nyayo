import re

from django import template
from django.conf import settings

from nyayo.actions.access import may_act

from .. import modify_test
from ..models import Test

register = template.Library()

OPENBENCH_ID = modify_test.OPENBENCH_ID
test_of = modify_test.test_of


@register.simple_tag(takes_context=True)
def match_actions(context, match):
    """The group *Actions for this match* at the bottom of the sidebar, on a match page and its game pages, for the people who
    may act: what `modify_test.offers` allows for a lab test's match (stop, restart, approve, unapprove, each a POST), and for
    any match *Remove* and *Delete*, which go to a page that says what will go. A removed match offers *Bring it back*."""
    request = context.get('request')
    if request is None or not may_act(request.user):
        return ''
    test = test_of(match)
    buttons = [] if test is None or match.hidden else [a for a in modify_test.offers(test) if modify_test.may(request.user, a)]
    return template.loader.render_to_string('lab/_match_actions.html', {'match': match, 'test': test, 'buttons': buttons}, request)


def producer_link(match):
    """The producer's link, unless it goes to OpenBench's server for a test that was never there."""
    url = (match.producer or {}).get('url')
    return url if url and (test_of(match) is not None or not OPENBENCH_ID.match(match.pk)) else None


@register.filter
def get(mapping, key):
    return mapping.get(key, "") if mapping else ""
