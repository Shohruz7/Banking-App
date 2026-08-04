"""Statement read and download endpoints (ADR-0021).

Owner scoping is the same rule the accounts app applies: someone else's statement is simply not in
the queryset, so it 404s rather than 403s. That matters more here than anywhere else in the API —
``MEDIA_URL`` is deliberately unrouted, so this view is the *only* path to a generated file, and it
is the only thing standing between a UUID and someone's monthly finances.
"""

from typing import Any

from django.db.models import QuerySet
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework.generics import ListAPIView
from rest_framework.request import Request
from rest_framework.views import APIView

from common.auth import request_user
from common.pagination import DefaultCursorPagination

from .models import Statement
from .serializers import StatementSerializer


class StatementCursorPagination(DefaultCursorPagination):
    """Newest generated first. ``Statement`` records ``generated_at`` rather than ``created_at``,
    which is the one field the default cursor class assumes."""

    ordering = "-generated_at"


class StatementListView(ListAPIView[Statement]):
    """``GET /api/v1/statements/`` — the requester's statements, newest first."""

    serializer_class = StatementSerializer
    pagination_class = StatementCursorPagination

    def get_queryset(self) -> QuerySet[Statement]:
        return Statement.objects.filter(user=request_user(self.request)).select_related("account")


class StatementDownloadView(APIView):
    """``GET /api/v1/statements/{id}/download/`` — stream the PDF.

    ``FileResponse`` streams through the storage backend rather than reading the file into memory,
    which is also what keeps this working unchanged when Week 8 points ``default`` at S3.
    """

    def get(self, request: Request, pk: Any) -> FileResponse:
        statement = get_object_or_404(Statement.objects.filter(user=request_user(request)), pk=pk)
        return FileResponse(
            statement.file.open("rb"),
            as_attachment=True,
            filename=f"statement-{statement.period_label}.pdf",
            content_type="application/pdf",
        )
