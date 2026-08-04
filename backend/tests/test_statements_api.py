"""Listing and downloading statements (ADR-0021).

``MEDIA_URL`` is deliberately unrouted, so the download view is the *only* path to a generated
file. Its ownership check is therefore the only thing between a UUID and someone's monthly
finances, which is why more than one test here is about a request that should fail.
"""

from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.urls import reverse
from freezegun import freeze_time
from rest_framework.test import APIClient

from statements.models import Statement, StatementKind
from statements.tasks import generate_monthly_statements
from tests.conftest import obtain_tokens
from tests.factories import AccountFactory, UserFactory, fund_account, post_balanced_entry

pytestmark = pytest.mark.django_db

JULY = "2026-07"


@pytest.fixture
def july_statement(password_user: User) -> Statement:
    cash = AccountFactory.create(owner=password_user, name="Everyday")
    other = AccountFactory.create(owner=password_user, name="Savings")
    with freeze_time("2026-06-25T09:00:00Z"):
        fund_account(cash, Decimal("900.0000"))
    with freeze_time("2026-07-08T09:00:00Z"):
        post_balanced_entry(cash, other, Decimal("55.0000"), description="coffee habit")
    generate_monthly_statements(JULY)
    return Statement.objects.get(account=cash)


def test_the_list_describes_each_statement_without_leaking_a_storage_path(
    auth_client: APIClient, july_statement: Statement
) -> None:
    response = auth_client.get(reverse("statement-list"))

    assert response.status_code == 200
    rows = response.json()["results"]
    mine = next(row for row in rows if row["id"] == str(july_statement.pk))
    assert mine["kind"] == StatementKind.CASH
    assert mine["period"] == "2026-07"
    assert mine["symbol_scope"] == "Everyday"
    assert mine["opening_balance"] == "900.0000"
    assert mine["closing_balance"] == "845.0000"
    assert mine["line_count"] == 1
    assert mine["download_url"] == f"/api/v1/statements/{july_statement.pk}/download/"
    # The storage path is not part of the contract and must not be guessable from the API.
    assert "file" not in mine


def test_downloading_streams_a_pdf_attachment(
    auth_client: APIClient, july_statement: Statement
) -> None:
    response = auth_client.get(reverse("statement-download", kwargs={"pk": july_statement.pk}))

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert "attachment" in response["Content-Disposition"]
    assert "statement-2026-07.pdf" in response["Content-Disposition"]
    # The DRF stubs type an APIClient response as the non-streaming variant; this view returns a
    # FileResponse, which streams — that is the whole point of it (ADR-0021).
    streamed = b"".join(response.streaming_content)  # type: ignore[attr-defined]
    assert streamed.startswith(b"%PDF")


def test_someone_elses_statement_is_a_404_not_a_403(
    api_client: APIClient, july_statement: Statement
) -> None:
    """A 403 would confirm the statement exists — the accounts app's rule, and it matters more
    here than anywhere else in the API."""
    stranger = UserFactory.create()
    tokens = obtain_tokens(APIClient(), stranger)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    assert (
        client.get(reverse("statement-download", kwargs={"pk": july_statement.pk})).status_code
        == 404
    )
    assert client.get(reverse("statement-list")).json()["results"] == []


def test_statements_require_authentication(
    api_client: APIClient, july_statement: Statement
) -> None:
    assert api_client.get(reverse("statement-list")).status_code == 401
    assert (
        api_client.get(reverse("statement-download", kwargs={"pk": july_statement.pk})).status_code
        == 401
    )


def test_the_list_is_cursor_paginated(auth_client: APIClient, password_user: User) -> None:
    cash = AccountFactory.create(owner=password_user, name="Everyday")
    with freeze_time("2026-01-05T09:00:00Z"):
        fund_account(cash, Decimal("1000.0000"))
    for month in ("2026-02", "2026-03", "2026-04"):
        generate_monthly_statements(month)

    body = auth_client.get(reverse("statement-list")).json()

    assert set(body) == {"next", "previous", "results"}
    assert len(body["results"]) == 3


# --------------------------------------------------------------------------------------------
# The management command
# --------------------------------------------------------------------------------------------


def test_the_command_generates_a_named_period(password_user: User) -> None:
    """How Week 8's seed will produce a year of statements, and how a missed month is backfilled."""
    cash = AccountFactory.create(owner=password_user, name="Everyday")
    with freeze_time("2026-07-04T09:00:00Z"):
        fund_account(cash, Decimal("300.0000"))

    out = StringIO()
    call_command("generate_statements", "--period", JULY, stdout=out)

    assert "2026-07" in out.getvalue()
    assert Statement.objects.filter(account=cash).exists()


def test_the_command_rejects_a_period_it_cannot_read() -> None:
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="YYYY-MM"):
        call_command("generate_statements", "--period", "last July")
