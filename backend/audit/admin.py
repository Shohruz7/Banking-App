"""Admin for the audit log — read-only, one step stricter than the ledger's.

``JournalEntryAdmin`` disables deletion; here add, change and delete are all disabled. Nothing may
create an audit row except :func:`audit.services.record_audit`, and nothing may alter one at all —
the trigger from migration 0002 would refuse anyway, but the UI should not offer the button.
"""

import json

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils.html import format_html

from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "actor_label", "target_type", "target_id", "ip")
    list_filter = ("action", "created_at")
    search_fields = ("actor_label", "target_id", "action", "request_id")
    date_hierarchy = "created_at"
    readonly_fields = (
        "id",
        "created_at",
        "action",
        "actor",
        "actor_label",
        "target_type",
        "target_id",
        "context_pretty",
        "ip",
        "user_agent",
        "request_id",
    )
    exclude = ("context",)

    @admin.display(description="Context")
    def context_pretty(self, obj: AuditEvent) -> str:
        return format_html("<pre>{}</pre>", json.dumps(obj.context, indent=2, sort_keys=True))

    def get_queryset(self, request: HttpRequest) -> QuerySet[AuditEvent]:
        return super().get_queryset(request).select_related("actor")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: AuditEvent | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: AuditEvent | None = None) -> bool:
        return False
