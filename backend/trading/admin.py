"""Admin for orders — a read-only window, like the ledger's.

Orders resolve through ``trading.services``, which posts the money movement and writes the audit
row. Editing a status here would change the record without changing the ledger, which is exactly
the drift the whole design is arranged to prevent.
"""

from django.contrib import admin
from django.http import HttpRequest

from .models import Order


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "user",
        "side",
        "quantity",
        "instrument",
        "order_type",
        "status",
        "filled_price",
    )
    list_filter = ("status", "side", "order_type")
    search_fields = ("user__username", "instrument__symbol", "idempotency_key")
    readonly_fields = tuple(field.name for field in Order._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Order | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Order | None = None) -> bool:
        return False
