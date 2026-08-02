"""Order and holdings endpoints.

Thin translation layers, following ``ledger.views.TransferView``: resolve identifiers to rows the
requester may touch, hand off to ``trading.services``, and map domain errors onto the ADR-0006
envelope. Every rule about trading lives in the service so it holds for the Celery sweep too, which
never comes through HTTP.
"""

from decimal import Decimal
from typing import Any, cast

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Account
from common.auth import request_user
from common.money import quantize_money
from ledger.api_exceptions import InsufficientFunds
from ledger.exceptions import InsufficientFundsError
from markets.models import Instrument

from .api_exceptions import (
    InstrumentInactive,
    InstrumentNotFound,
    InsufficientShares,
    InvalidOrder,
    OrderNotOpen,
)
from .exceptions import (
    InstrumentInactiveError,
    InsufficientSharesError,
    InvalidOrderError,
    OrderNotOpenError,
)
from .models import Order
from .serializers import HoldingSerializer, OrderCreateSerializer, OrderSerializer
from .services import cancel_order, place_order

_ZERO_SHARES = Decimal("0E-8")


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

    throttle_scope = "order"

    def get(self, request: Request) -> Response:
        orders = (
            Order.objects.filter(user=request_user(request))
            .select_related("instrument")
            .order_by("-created_at")
        )
        return Response(OrderSerializer(orders, many=True).data)

    def post(self, request: Request) -> Response:
        serializer = OrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data: dict[str, Any] = serializer.validated_data
        actor = request_user(request)

        instrument = Instrument.objects.filter(symbol=data["symbol"].upper()).first()
        if instrument is None:
            raise InstrumentNotFound
        # An account belonging to someone else is simply not in the queryset, so it 404s rather
        # than confirming it exists (the accounts app's rule, applied here).
        cash_account = get_object_or_404(
            Account.objects.filter(owner=actor), pk=data["cash_account"]
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

    def get(self, request: Request, pk: Any) -> Response:
        order = get_object_or_404(
            Order.objects.filter(user=request_user(request)).select_related("instrument"), pk=pk
        )
        return Response(OrderSerializer(order).data)


class OrderCancelView(APIView):
    """``POST /api/v1/orders/{id}/cancel/`` — withdraw a resting order."""

    throttle_scope = "order"

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

    Quantity and cost basis come from one annotated query over the position accounts, so N holdings
    still cost one query rather than N. Positions that have been fully sold are omitted: an account
    with zero shares and zero basis is history, not a holding.
    """

    def get(self, request: Request) -> Response:
        positions = (
            Account.objects.filter(owner=request_user(request))
            .positions()
            .select_related("instrument")
            .with_balance()
            .with_quantity()
        )

        rows: list[dict[str, object]] = []
        for account in positions:
            quantity: Decimal = account.quantity  # type: ignore[attr-defined]
            if quantity == _ZERO_SHARES:
                continue

            cost_basis: Decimal = account.balance  # type: ignore[attr-defined]
            # Non-null by construction: .positions() filters instrument__isnull=False.
            instrument = cast(Instrument, account.instrument)
            last_price = instrument.current_price
            rows.append(
                {
                    "symbol": instrument.symbol,
                    "name": instrument.name,
                    "account_id": account.pk,
                    "quantity": quantity,
                    "cost_basis": cost_basis,
                    "average_cost": quantize_money(cost_basis / quantity),
                    "last_price": last_price,
                    "market_value": quantize_money(last_price * quantity),
                }
            )

        # The DRF stubs type a plain Serializer's instance argument as the single-object type, so
        # they cannot express `many=True` (which returns a ListSerializer). The serializer is not
        # optional here: DRF's JSON encoder renders a bare Decimal as a float, and ADR-0009 says
        # money never crosses the wire as one.
        return Response(HoldingSerializer(rows, many=True).data)  # type: ignore[arg-type]
