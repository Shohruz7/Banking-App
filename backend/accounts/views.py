"""Account read endpoints.

Owner scoping happens in ``get_queryset``, not in a permission check: an account belonging to
someone else is simply not in the queryset, so it 404s. That is deliberate — a 403 would confirm
the account exists, which is an information leak.
"""

from django.db.models import QuerySet
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework.viewsets import ReadOnlyModelViewSet

from common.auth import request_user

from .models import Account
from .serializers import AccountDetailSerializer, AccountSerializer


@extend_schema_view(
    list=extend_schema(summary="Your money accounts, with derived balances"),
    retrieve=extend_schema(
        summary="One account, with its full account number",
        # The router builds this path from the router's own default, which spectacular cannot type
        # from a ViewSet whose queryset is request-scoped. Saying so beats defaulting to "string".
        parameters=[OpenApiParameter("id", OpenApiTypes.UUID, OpenApiParameter.PATH)],
    ),
)
class AccountViewSet(ReadOnlyModelViewSet[Account]):
    """List and retrieve the requesting user's money accounts, each with its derived balance.

    Position accounts are deliberately excluded (ADR-0016). Their balance is a *cost basis* in USD,
    not spendable cash, and listing the two side by side under one ``balance`` field would invite
    exactly that misreading — by a client, and by whoever writes the next feature. Holdings have
    their own endpoint, where the share count is shown alongside the basis that explains it.

    ``spendable()`` rather than ``cash()`` since Week 8: the latter also admitted the equity and
    income accounts that fund and absorb a user's own money, which turned this list into a menu of
    bookkeeping rows — and, because the transfer endpoint took its source from the same scope, into
    a way to spend them.
    """

    serializer_class = AccountSerializer

    def get_serializer_class(self) -> type[AccountSerializer]:
        """Full account number on retrieve, masked on list (ADR-0027).

        The split is the point: a list decrypts nothing, so the common read costs no AES and a bug
        that widens list access leaks four digits instead of ten.
        """
        return AccountDetailSerializer if self.action == "retrieve" else AccountSerializer

    def get_queryset(self) -> QuerySet[Account]:
        return Account.objects.filter(owner=request_user(self.request)).spendable().with_balance()
