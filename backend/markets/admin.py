"""Admin for market data — instruments editable, ticks read-only.

Instruments are reference data an operator legitimately curates (delisting a symbol, retuning a
simulation parameter). Ticks are machine output: they are produced by ``advance_prices`` and
nothing else, so the admin shows them and refuses to touch them.
"""

from django.contrib import admin
from django.http import HttpRequest

from .models import Instrument, PriceTick


@admin.register(Instrument)
class InstrumentAdmin(admin.ModelAdmin):
    list_display = ("symbol", "name", "sector", "last_price", "last_tick_at", "is_active")
    list_filter = ("is_active", "sector")
    search_fields = ("symbol", "name")
    readonly_fields = ("id", "last_price", "last_tick_at", "created_at")


@admin.register(PriceTick)
class PriceTickAdmin(admin.ModelAdmin):
    list_display = ("instrument", "price", "created_at")
    list_filter = ("instrument",)
    readonly_fields = ("instrument", "price", "created_at")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: PriceTick | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: PriceTick | None = None) -> bool:
        return False
