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
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Account
from audit.models import AuditAction
from audit.services import record_audit
from common.auth import request_user
from common.schema import error_response

from .api_exceptions import (
    DestinationNotFound,
    IdempotencyKeyConflict,
    InsufficientFunds,
    InvalidTransfer,
    SameAccount,
)
from .exceptions import IdempotencyKeyConflictError, InsufficientFundsError, InvalidEntryError
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
        # Scoped to spendable accounts as well as to the owner, matching what /accounts/ lists so
        # an id from that list is exactly an id that works here. Instrument accounts are excluded
        # because since ADR-0025 there are two kinds and the contra's history is a page of
        # zero-amount rows that mean nothing to a customer; holdings are read through /holdings/.
        # The equity and income accounts are excluded because they are the *other side* of the
        # customer's money — a ledger view of them belongs in the admin, not in a statement.
        account = get_object_or_404(
            Account.objects.filter(owner=request_user(self.request)).spendable(),
            pk=self.kwargs["pk"],
        )
        # The entry carries the description; without this the history is an amount and a UUID.
        return account.lines.select_related("entry")


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

    @extend_schema(
        request=TransferCreateSerializer,
        responses={
            201: JournalEntrySerializer,
            200: JournalEntrySerializer,
            400: error_response("insufficient_funds", "Not enough funds."),
            409: error_response("idempotency_key_conflict", "Key already used."),
        },
        summary="Move money between accounts",
        description=(
            "201 when the transfer posted, **200** when an idempotency key replayed one that "
            'had already posted — the status is how a client tells "I made this happen" from '
            '"this had already happened". A key reused for a *different* request is 409.'
        ),
    )
    def post(self, request: Request) -> Response:
        serializer = TransferCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data: dict[str, Any] = serializer.validated_data
        actor = request_user(request)

        # Source must be a spendable account of the requester's own; anything else is
        # indistinguishable from "no such account". `spendable()` and not merely `owner=` is
        # load-bearing: a realized loss sits *positive* in the user's income account, and without
        # this scope transferring it out converted that loss into money.
        source = get_object_or_404(
            Account.objects.filter(owner=actor).spendable(), pk=data["source_account"]
        )
        # Deliberately not owner-scoped — paying somebody else is the point — but money-scoped for
        # the same reason as the source: nobody's realized-P&L account is a payee.
        destination = Account.objects.spendable().filter(pk=data["destination_account"]).first()
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
        except IdempotencyKeyConflictError as exc:
            # Audited under its own action rather than as a rejection: the request broke no money
            # rule, it collided with a key the system had already bound to a different movement —
            # possibly another user's, since the key column is global (ADR-0024). The context
            # records the key and this requester's own accounts, never the entry it collided with.
            record_audit(
                action=AuditAction.TRANSFER_KEY_CONFLICT,
                actor=actor,
                target_type="account",
                target_id=str(source.pk),
                context={
                    "idempotency_key": data["idempotency_key"],
                    "amount": data["amount"],
                    "source_account": str(source.pk),
                    "destination_account": str(destination.pk),
                },
            )
            raise IdempotencyKeyConflict from exc
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
