"""Admin for the ledger — a read-only window onto an append-only structure (Week 2).

Entries and their lines are never edited or deleted through the admin: writes go through
``ledger.services.post_entry`` so the zero-sum invariant is validated. Deletion is disabled and
lines are shown read-only inline so an entry reads as one document.
"""

from django.contrib import admin
from django.http import HttpRequest

from .models import JournalEntry, JournalLine


class JournalLineInline(admin.TabularInline):
    model = JournalLine
    extra = 0
    can_delete = False
    readonly_fields = ("account", "amount", "currency", "created_at")

    def has_add_permission(self, request: HttpRequest, obj: JournalEntry | None = None) -> bool:
        return False


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ("id", "description", "created_at")
    search_fields = ("description",)
    readonly_fields = ("id", "created_at")
    inlines = (JournalLineInline,)

    def has_delete_permission(self, request: HttpRequest, obj: JournalEntry | None = None) -> bool:
        return False
