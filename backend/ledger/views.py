"""Ledger write and history endpoints.

The transfer endpoint is a thin translation layer and nothing more: it resolves account IDs to
rows the requester is allowed to touch, hands off to ``ledger.services.transfer``, and maps domain
errors onto the ADR-0006 envelope. Every rule about money — positive amounts, no self-transfers,
no overdrafts, idempotent replay — lives in the service, so it holds for callers that never come
through HTTP.
"""

from typing import Any

from django.contrib.auth.models import User
from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Account
from audit.models import AuditAction
from audit.services import record_audit
from common.auth import request_user

from .api_exceptions import DestinationNotFound, InsufficientFunds, InvalidTransfer, SameAccount
from .exceptions import InsufficientFundsError, InvalidEntryError
from .models import JournalLine
from .serializers import JournalEntrySerializer, JournalLineSerializer, TransferCreateSerializer
from .services import transfer


class AccountTransactionsView(ListAPIView[JournalLine]):
    """An owned account's journal lines, newest first, cursor-paginated.

    Ordered by ``-created_at`` (the pagination default), which is exactly the
    ``(account, created_at)`` composite index built in Week 2.
    """

    serializer_class = JournalLineSerializer

    def get_queryset(self) -> QuerySet[JournalLine]:
        account = get_object_or_404(
            Account.objects.filter(owner=request_user(self.request)), pk=self.kwargs["pk"]
        )
        return account.lines.all()


class TransferView(APIView):
    """``POST /api/v1/transfers/`` — move money between accounts.

    Returns 201 with the posted entry, or **200** when an idempotency key replays an entry a
    previous request already posted (ADR-0010). The status is how a client tells "I made this
    happen" from "this had already happened" without the retry being an error.

    Rejections are audited *here* rather than in the service, and that placement is load-bearing:
    ``InsufficientFundsError`` is raised inside ``transfer``'s ``transaction.atomic()`` block, so a
    row written at the raise site would be rolled back by the very exception it records. By the
    time these handlers run the transaction has unwound and the write sticks (ADR-0014).
    """

    throttle_scope = "transfer"

    def post(self, request: Request) -> Response:
        serializer = TransferCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data: dict[str, Any] = serializer.validated_data
        actor = request_user(request)

        # Source must be the requester's own account; anything else is indistinguishable from
        # "no such account".
        source = get_object_or_404(Account.objects.filter(owner=actor), pk=data["source_account"])
        destination = Account.objects.filter(pk=data["destination_account"]).first()
        if destination is None:
            raise DestinationNotFound
        if source.pk == destination.pk:
            raise SameAccount

        try:
            entry, created = transfer(
                source=source,
                destination=destination,
                amount=data["amount"],
                description=data["description"],
                idempotency_key=data["idempotency_key"],
                actor=actor,
            )
        except InsufficientFundsError as exc:
            self._audit_rejection(actor, source, destination, data, "insufficient_funds")
            raise InsufficientFunds(str(exc)) from exc
        except InvalidEntryError as exc:
            self._audit_rejection(actor, source, destination, data, "invalid_transfer")
            raise InvalidTransfer(str(exc)) from exc

        body = JournalEntrySerializer(entry).data
        return Response(body, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @staticmethod
    def _audit_rejection(
        actor: User,
        source: Account,
        destination: Account,
        data: dict[str, Any],
        reason: str,
    ) -> None:
        record_audit(
            action=AuditAction.TRANSFER_REJECTED,
            actor=actor,
            target_type="account",
            target_id=str(source.pk),
            context={
                "reason": reason,
                "amount": data["amount"],
                "source_account": str(source.pk),
                "destination_account": str(destination.pk),
            },
        )
