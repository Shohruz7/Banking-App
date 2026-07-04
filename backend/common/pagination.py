"""Cursor pagination, newest first — the one pagination style for the API (ADR-0006)."""

from rest_framework.pagination import CursorPagination


class DefaultCursorPagination(CursorPagination):
    page_size = 20
    ordering = "-created_at"
