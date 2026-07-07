"""Admin for accounts — read-mostly, with a derived balance column (Week 2).

Balance is annotated in a single query rather than computed per row, so the changelist stays one
query regardless of account count.
"""

from decimal import Decimal

from django.contrib import admin
from django.db.models import DecimalField, QuerySet, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpRequest

from .models import Account


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "account_type", "currency", "balance", "created_at")
    list_filter = ("account_type", "currency")
    search_fields = ("name", "owner__username")
    readonly_fields = ("id", "created_at")

    def get_queryset(self, request: HttpRequest) -> QuerySet[Account]:
        return (
            super()
            .get_queryset(request)
            .select_related("owner")
            .annotate(
                _balance=Coalesce(
                    Sum("lines__amount"),
                    Value(
                        Decimal("0.0000"),
                        output_field=DecimalField(max_digits=20, decimal_places=4),
                    ),
                )
            )
        )

    @admin.display(description="Balance", ordering="_balance")
    def balance(self, obj: Account) -> Decimal:
        return obj._balance  # type: ignore[attr-defined]  # annotated in get_queryset
