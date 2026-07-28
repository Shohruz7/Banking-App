"""Account serializers — read-only projections of the ledger's account model."""

from rest_framework import serializers

from .models import Account


class AccountSerializer(serializers.ModelSerializer[Account]):
    """An account with its derived balance.

    ``balance`` is not a model field: it comes from the queryset annotation in ``AccountViewSet``,
    so a list of N accounts still costs one query. Declared explicitly (rather than left to
    ``ModelSerializer`` inference) so the decimal places match the storage contract of ADR-0009 —
    and so it serializes as the string ``"150.0000"``, never a float.
    """

    balance = serializers.DecimalField(max_digits=20, decimal_places=4, read_only=True)

    class Meta:
        model = Account
        fields = ("id", "name", "account_type", "currency", "balance", "created_at")
        read_only_fields = fields
