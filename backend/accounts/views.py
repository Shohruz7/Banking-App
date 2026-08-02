"""Account read endpoints.

Owner scoping happens in ``get_queryset``, not in a permission check: an account belonging to
someone else is simply not in the queryset, so it 404s. That is deliberate — a 403 would confirm
the account exists, which is an information leak.
"""

from django.db.models import QuerySet
from rest_framework.viewsets import ReadOnlyModelViewSet

from common.auth import request_user

from .models import Account
from .serializers import AccountSerializer


class AccountViewSet(ReadOnlyModelViewSet[Account]):
    """List and retrieve the requesting user's money accounts, each with its derived balance.

    Position accounts are deliberately excluded (ADR-0016). Their balance is a *cost basis* in USD,
    not spendable cash, and listing the two side by side under one ``balance`` field would invite
    exactly that misreading — by a client, and by whoever writes the next feature. Holdings have
    their own endpoint, where the share count is shown alongside the basis that explains it.
    """

    serializer_class = AccountSerializer

    def get_queryset(self) -> QuerySet[Account]:
        return Account.objects.filter(owner=request_user(self.request)).cash().with_balance()
