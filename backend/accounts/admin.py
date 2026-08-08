"""Admin for accounts — read-mostly, with a derived balance column (Week 2).

Balance is annotated in a single query rather than computed per row, so the changelist stays one
query regardless of account count.
"""

from decimal import Decimal
from typing import cast

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from .models import Account, AccountQuerySet


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "account_type", "currency", "balance", "created_at")
    list_filter = ("account_type", "currency")
    search_fields = ("name", "owner__username")
    readonly_fields = ("id", "created_at")

    def get_queryset(self, request: HttpRequest) -> QuerySet[Account]:
        # ModelAdmin.get_queryset is typed as a plain QuerySet; the model's manager is built from
        # AccountQuerySet, so the balance annotation is genuinely there.
        queryset = cast(AccountQuerySet, super().get_queryset(request))
        return queryset.select_related("owner").with_balance()

    @admin.display(description="Balance", ordering="balance")
    def balance(self, obj: Account) -> Decimal:
        # `balance` is a queryset annotation, not a field, so the model has no such attribute as
        # far as the type checker is concerned; the cast records both that and its real type.
        return cast(Decimal, obj.balance)  # type: ignore[attr-defined]
