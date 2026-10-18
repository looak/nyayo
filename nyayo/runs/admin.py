from django.contrib import admin, messages

from nyayo.paging import PAGE_SIZE

from .ingest import delete_archived
from .models import Machine, Run


@admin.register(Machine)
class MachineAdmin(admin.ModelAdmin):
    list_per_page = PAGE_SIZE
    list_display = ["name", "active", "created_at", "run_count"]
    readonly_fields = ["created_at"]
    actions = ["revoke", "rotate_token"]

    @admin.display(description="runs")
    def run_count(self, machine):
        return machine.runs.count()

    def save_model(self, request, machine, form, change):
        if not change:
            token = machine.new_token()
            super().save_model(request, machine, form, change)
            self.show_token(request, machine, token)
        else:
            super().save_model(request, machine, form, change)

    def show_token(self, request, machine, token):
        messages.warning(request, f"Token for {machine.name}, shown once, copy it now: {token}  "
                                  f"(on that machine: git config nyayo.token {token})")

    @admin.action(description="Revoke the token of the selected machines")
    def revoke(self, request, queryset):
        queryset.update(active=False)

    @admin.action(description="Give the selected machines a new token (the old one stops working)")
    def rotate_token(self, request, queryset):
        for machine in queryset:
            token = machine.new_token()
            machine.active = True
            machine.save()
            self.show_token(request, machine, token)


@admin.register(Run)
class RunAdmin(admin.ModelAdmin):
    list_per_page = PAGE_SIZE
    list_display = ["short_name", "state", "branch", "machine", "host_name", "recorded_at"]
    list_filter = ["state", "kind", "source", "machine", "branch"]
    search_fields = ["id", "commit", "tree", "branch"]
    actions = ["delete_with_archive"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, run=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def delete_model(self, request, run):
        delete_archived(run.pk)
        super().delete_model(request, run)

    @admin.action(description="Delete the selected runs and their archived files")
    def delete_with_archive(self, request, queryset):
        for run in queryset:
            delete_archived(run.pk)
        queryset.delete()
