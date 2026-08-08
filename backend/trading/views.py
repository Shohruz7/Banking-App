"""Order and holdings endpoints.

Thin translation layers, following ``ledger.views.TransferView``: resolve identifiers to rows the
requester may touch, hand off to ``trading.services``, and map domain errors onto the ADR-0006
envelope. Every rule about trading lives in the service so it holds for the Celery sweep too, which
never comes through HTTP.
"""

from typing import Any

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import BaseThrottle
from rest_framework.views import APIView

from accounts.models import Account
from audit.models import AuditAction
from audit.services import record_audit
from common.auth import request_user
from common.pagination import DefaultCursorPagination
from common.schema import error_response
from ledger.api_exceptions import InsufficientFunds
from ledger.exceptions import InsufficientFundsError
from markets.models import Instrument

from .api_exceptions import (
    InstrumentInactive,
    InstrumentNotFound,
    InsufficientShares,
    InvalidOrder,
    OrderKeyConflict,
    OrderNotOpen,
)
from .exceptions import (
    InstrumentInactiveError,
    InsufficientSharesError,
    InvalidOrderError,
    OrderKeyConflictError,
    OrderNotOpenError,
)
from .models import Order
from .portfolio import Holding, holdings_for, portfolio_for
from .serializers import (
    HoldingSerializer,
    OrderCreateSerializer,
    OrderSerializer,
    PortfolioSerializer,
)
from .services import cancel_order, place_order


class OrderListCreateView(APIView):
    """``GET /api/v1/orders/`` and ``POST /api/v1/orders/``.

    A market order returns 201 already ``filled``; a limit order returns 201 ``open`` and waits for
    the market. Both are 201: the order was created either way, and the ``status`` field — not the
    HTTP code — is what says whether money moved.

    A failed fill is recorded on the order by ``place_order`` itself, which is safe because
    ``execute_fill``'s transaction has already unwound by the time it returns (ADR-0014). This view
    only maps the resulting exception onto the ADR-0006 envelope, so an order rejected over HTTP and
    one rejected by the Celery sweep are recorded identically.
    """

    def get_throttles(self) -> list[BaseThrottle]:
        """Reading orders and placing them are different budgets.

        One `throttle_scope` on the class charged a GET against the 30/min *placement* allowance, so
        a client polling its own order list could throttle itself out of trading. Placement is the
        scarce operation; listing is an ordinary read and falls back to the user ceiling.
        """
        self.throttle_scope = "order" if self.request.method == "POST" else "user"
        return super().get_throttles()

    @extend_schema(
        responses=OrderSerializer(many=True),
        summary="List your orders, newest first",
        # Explicit, because two different views both generate "orders_retrieve" from the URL
        # and spectacular would otherwise disambiguate them with a numeral suffix.
        operation_id="orders_list",
    )
    def get(self, request: Request) -> Response:
        """Cursor-paginated, newest first.

        Unbounded until Week 7: this returned every order the user had ever placed, in one response,
        growing forever. It was the endpoint most likely to be the first to time out.
        """
        orders = (
            Order.objects.filter(user=request_user(request))
            .select_related("instrument")
            .order_by("-created_at")
        )
        paginator = DefaultCursorPagination()
        page = paginator.paginate_queryset(orders, request, view=self)
        return paginator.get_paginated_response(OrderSerializer(page, many=True).data)

    @extend_schema(
        request=OrderCreateSerializer,
        responses={
            201: OrderSerializer,
            400: error_response("insufficient_funds", "Not enough funds."),
            409: error_response("order_key_conflict", "Key already used."),
        },
        summary="Place an order",
        description=(
            "A market order returns 201 already `filled`; a limit order returns 201 `open` and "
            "rests until the market crosses it. Both are 201 — `status`, not the HTTP code, is "
            "what says whether money moved."
        ),
    )
    def post(self, request: Request) -> Response:
        serializer = OrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data: dict[str, Any] = serializer.validated_data
        actor = request_user(request)

        instrument = Instrument.objects.filter(symbol=data["symbol"].upper()).first()
        if instrument is None:
            raise InstrumentNotFound
        # An account belonging to someone else is simply not in the queryset, so it 404s rather
        # than confirming it exists (the accounts app's rule, applied here). `spendable()` closes
        # the brokerage door on the same hole the transfer endpoint had: `place_order` refuses a
        # *position* account but not an equity or income one, so a realized loss could have been
        # spent on shares even after it could no longer be transferred out.
        cash_account = get_object_or_404(
            Account.objects.filter(owner=actor).spendable(), pk=data["cash_account"]
        )

        try:
            order = place_order(
                user=actor,
                instrument=instrument,
                cash_account=cash_account,
                side=data["side"],
                order_type=data["order_type"],
                quantity=data["quantity"],
                limit_price=data["limit_price"],
                idempotency_key=data["idempotency_key"],
            )
        except OrderKeyConflictError as exc:
            # Audited before the 409 leaves, and with this requester's own request in the context —
            # never the order it collided with, which may not be theirs to see (ADR-0024).
            record_audit(
                action=AuditAction.ORDER_KEY_CONFLICT,
                actor=actor,
                target_type="account",
                target_id=str(cash_account.pk),
                context={
                    "idempotency_key": data["idempotency_key"],
                    "symbol": instrument.symbol,
                    "side": data["side"],
                    "quantity": data["quantity"],
                },
            )
            raise OrderKeyConflict from exc
        except InstrumentInactiveError as exc:
            raise InstrumentInactive(str(exc)) from exc
        except InsufficientFundsError as exc:
            raise InsufficientFunds(str(exc)) from exc
        except InsufficientSharesError as exc:
            raise InsufficientShares(str(exc)) from exc
        except InvalidOrderError as exc:
            raise InvalidOrder(str(exc)) from exc

        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)


class OrderDetailView(APIView):
    """``GET /api/v1/orders/{id}/`` — one of the requester's own orders."""

    @extend_schema(
        responses=OrderSerializer,
        summary="Retrieve one of your orders",
        operation_id="orders_retrieve",
    )
    def get(self, request: Request, pk: Any) -> Response:
        order = get_object_or_404(
            Order.objects.filter(user=request_user(request)).select_related("instrument"), pk=pk
        )
        return Response(OrderSerializer(order).data)


class OrderCancelView(APIView):
    """``POST /api/v1/orders/{id}/cancel/`` — withdraw a resting order."""

    throttle_scope = "order"

    @extend_schema(
        request=None,
        responses={
            200: OrderSerializer,
            409: error_response("order_not_open", "That order is no longer open."),
        },
        summary="Cancel a resting order",
    )
    def post(self, request: Request, pk: Any) -> Response:
        actor = request_user(request)
        order = get_object_or_404(
            Order.objects.filter(user=actor).select_related("instrument"), pk=pk
        )
        try:
            order = cancel_order(order, actor=actor)
        except OrderNotOpenError as exc:
            raise OrderNotOpen(str(exc)) from exc
        return Response(OrderSerializer(order).data)


class HoldingsView(APIView):
    """``GET /api/v1/holdings/`` — every instrument the requester holds.

    All of the arithmetic lives in :func:`trading.portfolio.holdings_for`, which the portfolio
    endpoint and the brokerage statement also call. Two implementations of "what is this position
    worth" is exactly the drift ADR-0016 built derived holdings to avoid.
    """

    @extend_schema(responses=HoldingSerializer(many=True), summary="Your holdings")
    def get(self, request: Request) -> Response:
        holdings = holdings_for(request_user(request))
        # Bounded by the instrument count rather than by history, so this is not the urgent case
        # `/orders/` was — but "bounded by how many symbols exist" is not a bound the API should
        # promise, and paginating now means the shape does not change when it matters.
        paginator = LimitOffsetPagination()
        paginator.default_limit = 50
        # A list, not a queryset: holdings are computed, not selected. LimitOffsetPagination slices
        # any sequence, which is why it is the right paginator here — cursor pagination needs an
        # ordering column on a model, and a Holding is a dataclass.
        page: list[Holding] = paginator.paginate_queryset(holdings, request, view=self)  # type: ignore[arg-type,assignment]
        # The DRF stubs type a plain Serializer's instance argument as the single-object type, so
        # they cannot express `many=True` (which returns a ListSerializer). The serializer is not
        # optional here: DRF's JSON encoder renders a bare Decimal as a float, and ADR-0009 says
        # money never crosses the wire as one.
        data = HoldingSerializer(page, many=True).data  # type: ignore[arg-type]
        return paginator.get_paginated_response(data)


class PortfolioView(APIView):
    """``GET /api/v1/portfolio/`` — cash, holdings, and what the difference is worth (ADR-0020).

    Every figure is derived from ledger rows at request time; the endpoint adds no state of its own
    and its correctness is therefore a property of the postings, not of anything it stores.
    """

    @extend_schema(responses=PortfolioSerializer, summary="Cash, holdings and total value")
    def get(self, request: Request) -> Response:
        return Response(PortfolioSerializer(portfolio_for(request_user(request))).data)
