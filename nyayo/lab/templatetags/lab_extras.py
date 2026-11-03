import re

from django import template
from django.conf import settings

from nyayo.actions.access import may_act

from .. import modify_test
from ..models import Test

register = template.Library()

OPENBENCH_ID = re.compile(r'^openbench-(?P<instance>.+)-(?P<number>\d+)$')


def test_of(match):
    """The lab's test a match is the document of, or None: an old match, from OpenBench's own server, has none."""
    found = OPENBENCH_ID.match(match.pk)
    if not found or found['instance'] != settings.NYAYO_INSTANCE:
        return None
    return Test.objects.filter(pk=int(found['number'])).first()


@register.simple_tag(takes_context=True)
def test_actions(context, match):
    """Stop, restart and approve, on the page of a match that is a lab test's, for the people who may act."""
    request = context.get('request')
    test = test_of(match)
    if test is None or request is None or not may_act(request.user):
        return ''
    buttons = [action for action in modify_test.offers(test) if modify_test.may(request.user, action)]
    return template.loader.render_to_string('lab/_test_actions.html', {'test': test, 'buttons': buttons}, request)


def producer_link(match):
    """The producer's link, unless it goes to OpenBench's server for a test that was never there."""
    url = (match.producer or {}).get('url')
    return url if url and (test_of(match) is not None or not OPENBENCH_ID.match(match.pk)) else None


@register.filter
def get(mapping, key):
    return mapping.get(key, "") if mapping else ""
