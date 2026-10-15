from django.contrib import admin

from . import games
from .ingest import delete_archived
from .models import Engine, Match


@admin.register(Engine)
class EngineAdmin(admin.ModelAdmin):
    list_display = ["name", "version", "commit"]
    search_fields = ["name", "version", "commit"]


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "kind", "state", "subject", "producer_name", "machine", "updated_at"]
    list_filter = ["kind", "state", "producer_name", "machine"]
    search_fields = ["id", "name"]
    actions = ["delete_with_archive"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, match=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def delete_model(self, request, match):
        delete_archived(match.pk)
        games.delete_files(match.pk)
        super().delete_model(request, match)

    @admin.action(description="Delete the selected matches and their archived files")
    def delete_with_archive(self, request, queryset):
        for match in queryset:
            delete_archived(match.pk)
            games.delete_files(match.pk)
        queryset.delete()
