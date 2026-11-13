"""Making a test from a script: `POST /api/tests` and `GET /api/tests/presets`, signed in with an ApiToken. The checks are
the page's (create_test.create_test); this only turns JSON into the page's values and the page's answer into JSON."""

import json
from functools import wraps

from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from nyayo.actions.access import may_act

from . import api_tokens, create_test as creating, modify_test
from .models import EngineConfig


def token_required(view):
    @csrf_exempt
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        token = api_tokens.by_header(request.headers.get('Authorization'))
        if token is None:
            response = JsonResponse({'errors': ['Missing or bad token: send Authorization: Bearer <token>']}, status=401)
            response['WWW-Authenticate'] = 'Bearer'
            return response
        if not may_act(token.user):
            return JsonResponse({'errors': ["This token's user may not make tests"]}, status=403)
        request.api_user = token.user
        return view(request, *args, **kwargs)
    return wrapper


def engine_named(name):
    """(config, None), or (None, the 400's errors) with the names that are valid."""
    names = list(EngineConfig.objects.filter(enabled=True).order_by('name').values_list('name', flat=True))
    config = EngineConfig.objects.filter(name=name, enabled=True).first() if isinstance(name, str) else None
    if config is None:
        return None, ['Unknown engine %r. Engines: %s' % (name, ', '.join(names) or 'none')]
    return config, None


@token_required
@require_http_methods(['GET'])
def presets(request):
    config, errors = engine_named(request.GET.get('engine', ''))
    if errors:
        return JsonResponse({'errors': errors}, status=400)
    table = creating.engine_table()[config.name]
    return JsonResponse({'engine': config.name, 'default': table['default'], 'presets': table['presets']})


def text(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return '1' if value else ''
    return str(value).strip()


@token_required
@require_http_methods(['POST'])
def create(request):
    try:
        body = json.loads(request.body or b'')
        assert isinstance(body, dict)
    except (ValueError, AssertionError):
        return JsonResponse({'errors': ['The body must be a JSON object']}, status=400)

    config, errors = engine_named(body.get('engine'))
    if errors:
        return JsonResponse({'errors': errors}, status=400)

    names = creating.preset_names(config)
    preset = body.get('preset')
    if preset not in names:
        return JsonResponse({'errors': ['Unknown preset %r for %s. Presets: %s' % (preset, config.name, ', '.join(names) or 'none')]}, status=400)

    # The page's values: the engine's defaults, the preset, then what was sent
    values = creating.default_values(config, preset)
    unknown = [key for key in body if key not in values and key != 'anyway']
    if unknown:
        return JsonResponse({'errors': ['Unknown field %s' % ', '.join(sorted(unknown))]}, status=400)
    if body.get('test_mode') == creating.TUNE:
        return JsonResponse({'errors': ['A tune is not made here']}, status=400)
    values.update({key: text(body[key]) for key in values if key in body})
    anyway = body.get('anyway') is True

    test, errors, behind = creating.create_test(values, request.api_user, anyway=anyway)
    warnings = []
    if behind:
        warnings.append('%s is %d commit%s behind %s.' % (behind['dev'], behind['commits'], '' if behind['commits'] == 1 else 's', behind['base']))
    if test is None:
        messages = list(errors.values()) or ['Not created: %s Send "anyway": true to create it.' % ' '.join(warnings)]
        return JsonResponse({'errors': messages, 'warnings': warnings}, status=400)

    modify_test.report(test)
    match = test.match_id()
    return JsonResponse({'id': test.id, 'match': match, 'url': request.build_absolute_uri(reverse('match', args=[match])),
                         'warnings': warnings}, status=201)
