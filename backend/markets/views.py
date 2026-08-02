"""Instrument read endpoints.

Instruments are reference data, not user data: unlike accounts there is nothing to owner-scope, so
these are plain authenticated reads with no per-user filtering. Prices are cursor-paginated newest
first (ADR-0006), which is exactly the ``(instrument, -created_at)`` index.
"""

from django.db.models import Q, QuerySet
from rest_framework.generics import ListAPIView
from rest_framework.viewsets import ReadOnlyModelViewSet

from .models import Instrument, PriceTick
from .serializers import InstrumentSerializer, PriceTickSerializer


class InstrumentViewSet(ReadOnlyModelViewSet[Instrument]):
    """List and retrieve tradeable instruments, looked up by symbol.

    The symbol is the lookup key rather than the UUID PK: it is unique, stable, and the thing a
    human actually types. ADR-0005's non-enumerable-identifier reasoning does not apply here —
    instruments are public reference data, and the whole list is one request away by design.
    """

    serializer_class = InstrumentSerializer
    lookup_field = "symbol"
    # Real tickers carry dots and hyphens (BRK.B, RDS-A); the default regex would reject them.
    lookup_value_regex = "[A-Za-z0-9.-]+"

    def get_queryset(self) -> QuerySet[Instrument]:
        # Delisted instruments stay retrievable by symbol — orders and audit rows reference them —
        # but they are kept out of the browsable list, where they would only invite dead orders.
        queryset = Instrument.objects.all()
        if self.action == "list":
            queryset = queryset.filter(is_active=True)

        search = self.request.query_params.get("q")
        if search:
            queryset = queryset.filter(Q(symbol__icontains=search) | Q(name__icontains=search))
        return queryset

    def get_object(self) -> Instrument:
        # Symbols are stored upper-cased, so /instruments/aapl/ has to resolve to AAPL.
        self.kwargs[self.lookup_field] = self.kwargs[self.lookup_field].upper()
        return super().get_object()


class InstrumentPricesView(ListAPIView[PriceTick]):
    """``GET /api/v1/instruments/{symbol}/prices/`` — tick history, newest first."""

    serializer_class = PriceTickSerializer

    def get_queryset(self) -> QuerySet[PriceTick]:
        return PriceTick.objects.filter(instrument__symbol=self.kwargs["symbol"].upper())
