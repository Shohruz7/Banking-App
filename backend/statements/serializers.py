"""Statement serializers — the index a client browses before downloading anything."""

from rest_framework import serializers

from .models import Statement


class StatementSerializer(serializers.ModelSerializer[Statement]):
    """One generated statement.

    The file itself is deliberately absent: a URL here would be a second way to reach the bytes,
    and the download view's ownership check is meant to be the only one (ADR-0021). ``download_url``
    is the route, not the storage path.
    """

    period = serializers.CharField(source="period_label", read_only=True)
    symbol_scope = serializers.SerializerMethodField()
    download_url = serializers.SerializerMethodField()
    opening_balance = serializers.DecimalField(max_digits=20, decimal_places=4, read_only=True)
    closing_balance = serializers.DecimalField(max_digits=20, decimal_places=4, read_only=True)

    class Meta:
        model = Statement
        fields = (
            "id",
            "kind",
            "period",
            "period_start",
            "period_end",
            "account_id",
            "symbol_scope",
            "opening_balance",
            "closing_balance",
            "line_count",
            "download_url",
            "generated_at",
        )
        read_only_fields = fields

    def get_symbol_scope(self, obj: Statement) -> str:
        """What the statement covers, in one phrase a client can render as a title."""
        # Non-null whenever account_id is set — the CHECK constraint on Statement says so.
        return obj.account.name if obj.account is not None else "Brokerage"

    def get_download_url(self, obj: Statement) -> str:
        return f"/api/v1/statements/{obj.pk}/download/"
