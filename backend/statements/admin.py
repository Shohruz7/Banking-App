"""Admin for generated statements — visible, never editable.

Machine output, like ``PriceTick``: statements are produced by ``statements.tasks`` and nothing
else. Editing a closing balance in the admin would change the row while the PDF it describes went
on saying something different, which is the one failure a stored artifact must not have.
"""

from django.contrib import admin
from django.http import HttpRequest

from .models import Statement


@admin.register(Statement)
class StatementAdmin(admin.ModelAdmin):
    list_display = (
        "period_label",
        "kind",
        "user",
        "account",
        "opening_balance",
        "closing_balance",
        "line_count",
        "generated_at",
    )
    list_filter = ("kind", "period_start")
    search_fields = ("user__username", "account__name")
    date_hierarchy = "period_start"
    readonly_fields = (
        "id",
        "user",
        "account",
        "kind",
        "period_start",
        "period_end",
        "opening_balance",
        "closing_balance",
        "line_count",
        "file",
        "generated_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Statement | None = None) -> bool:
        return False
