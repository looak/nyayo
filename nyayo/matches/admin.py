from django.contrib import admin, messages

from nyayo.paging import PAGE_SIZE

from . import games
from .ingest import delete_archived
from .models import Batch, Engine, Match, Upload


@admin.register(Engine)
class EngineAdmin(admin.ModelAdmin):
    list_per_page = PAGE_SIZE
    list_display = ["name", "version", "commit"]
    search_fields = ["name", "version", "commit"]


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    list_per_page = PAGE_SIZE
    list_display = ["id", "name", "kind", "state", "subject", "producer_name", "machine", "updated_at"]
    list_filter = ["kind", "state", "producer_name", "machine"]
    search_fields = ["id", "name"]
    actions = ["delete_with_archive"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, match=None):
        return False

    def has_delete_permission(self, request, match=None):
        # a match with batches of games is data to keep: purge_batches by hand first
        return match is None or not (match.batches.exists() or match.uploads.exists())

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
        kept = [m.pk for m in queryset if m.batches.exists() or m.uploads.exists()]
        if kept:
            self.message_user(request, f"Not deleted, they have batches of games (manage.py purge_batches first): "
                                       f"{', '.join(kept)}", level=messages.ERROR)
        queryset = queryset.exclude(pk__in=kept)
        for match in queryset:
            delete_archived(match.pk)
            games.delete_files(match.pk)
        queryset.delete()


class ReadOnly(admin.ModelAdmin):
    list_per_page = PAGE_SIZE

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Upload)
class UploadAdmin(ReadOnly):
    """Uploads of finished games as the worker left them; a refused one says why."""

    list_display = ["match", "name", "state", "games", "appended", "reason", "machine", "received_at", "done_at"]
    list_filter = ["state", "machine"]
    search_fields = ["match__id", "name", "reason"]
    actions = ["read_again"]

    @admin.action(description="Read the selected refused uploads again")
    def read_again(self, request, queryset):
        count = queryset.filter(state="refused").update(state="pending", reason="", games=0, appended=0,
                                                        done_at=None)
        self.message_user(request, f"{count} upload{'s' if count != 1 else ''} queued for the worker again")


@admin.register(Batch)
class BatchAdmin(ReadOnly):
    list_display = ["match", "number", "state", "games", "bytes", "stored_bytes", "reader", "created_at",
                    "sealed_at"]
    list_filter = ["state", "reader"]
    search_fields = ["match__id"]
