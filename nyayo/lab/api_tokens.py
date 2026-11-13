"""Tokens for scripts that sign in as a user (Elephant's test/ob_sprt.py makes tests with one). The owner makes them with
`manage.py api_token`; only the sha256 is kept, as a worker's token is (registration.digest). See docs/spec.md, *Lab: the
test API*."""

import secrets

from django.utils import timezone

from .models import ApiToken
from .registration import digest


def make(name, user):
    """(token, ApiToken): the token is shown once and is not kept."""
    token = secrets.token_urlsafe(32)
    return token, ApiToken.objects.create(name=name, token_hash=digest(token), user=user)


def by_header(header):
    """The ApiToken of an `Authorization: Bearer <token>` header, or None when it is missing, wrong, revoked or its user
    can't sign in. Marks it used."""
    scheme, _, token = (header or '').partition(' ')
    token = token.strip()
    if scheme.lower() != 'bearer' or not token:
        return None
    row = ApiToken.objects.select_related('user').filter(token_hash=digest(token), revoked__isnull=True).first()
    if row is None or not row.user.is_active:
        return None
    ApiToken.objects.filter(pk=row.pk).update(last_used=timezone.now())
    return row


def revoke(rows):
    return rows.filter(revoked__isnull=True).update(revoked=timezone.now())
