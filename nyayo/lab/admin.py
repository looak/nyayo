from django.contrib import admin
from django.utils import timezone

from .models import (ApiToken, Place, Book, Engine, EngineConfig, LogEvent, Machine, Network, Result, ServerState,
                     SPSAParameter, SPSARun, Step, Test)

for model in (Place, Test, Result, Step, Machine, Engine, EngineConfig, Book, Network, SPSARun, SPSAParameter, LogEvent,
              ServerState):
    admin.site.register(model)


@admin.register(ApiToken)
class ApiTokenAdmin(admin.ModelAdmin):
    # Read only: tokens are made with `manage.py api_token create`, and the admin can only revoke them
    list_display = ('name', 'user', 'created', 'last_used', 'revoked')
    actions = ('revoke',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.action(description='Revoke the chosen tokens')
    def revoke(self, request, queryset):
        count = queryset.filter(revoked__isnull=True).update(revoked=timezone.now())
        self.message_user(request, '%d token%s revoked.' % (count, '' if count == 1 else 's'))
