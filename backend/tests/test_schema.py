"""The API describes itself, accurately (ADR-0028).

The README's hand-maintained endpoint table had already drifted from the code — it did not mention
that ``GET /orders/`` was unpaginated, or the ``?q=`` parameter on ``/instruments/``. A generated
schema cannot drift. But a schema that silently omits half the API is worse than a stale table,
because it *looks* authoritative, so these tests assert on what is in it rather than that it exists.
"""

from typing import Any, cast

import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


def _schema() -> dict[str, Any]:
    from drf_spectacular.generators import SchemaGenerator

    return cast(dict[str, Any], SchemaGenerator().get_schema(request=None, public=True))


def test_the_schema_generates_without_errors(capsys: pytest.CaptureFixture[str]) -> None:
    """Generation warnings are the failure mode that matters.

    Spectacular degrades rather than raising: a view it cannot introspect is dropped from the
    document with a message on stderr and the command still exits 0. So the assertion is on the
    output, not the exit code — capable of failing because an un-annotated ``APIView`` prints
    "unable to guess serializer" and this test reads it.
    """
    call_command("spectacular", "--file", "/dev/null")
    captured = capsys.readouterr()
    assert "Error" not in captured.err
    assert "unable to guess serializer" not in captured.err


def test_every_endpoint_is_present() -> None:
    """A spot check that the document covers the API rather than the half of it DRF can infer."""
    paths = _schema()["paths"]

    for path in (
        "/api/v1/accounts/",
        "/api/v1/accounts/{id}/",
        "/api/v1/transfers/",
        "/api/v1/orders/",
        "/api/v1/orders/{id}/",
        "/api/v1/holdings/",
        "/api/v1/portfolio/",
        "/api/v1/statements/",
        "/api/v1/auth/token/",
        "/api/v1/auth/mfa/enroll/",
        "/api/v1/health/",
        "/api/v1/ready/",
    ):
        assert path in paths, f"{path} missing from the schema"


def test_authenticated_endpoints_are_documented_as_requiring_a_token() -> None:
    """Capable of failing against an unregistered authentication extension.

    Spectacular resolves authentication classes by exact type. This project's is a subclass it has
    never seen, so before the extension in ``common.schema`` every protected endpoint was documented
    as public — an error a client only discovers at runtime, as a 401.
    """
    schema = _schema()
    assert "jwtAuth" in schema["components"]["securitySchemes"]
    assert schema["paths"]["/api/v1/portfolio/"]["get"]["security"] == [{"jwtAuth": []}]
    # Registration is genuinely public, and the document says so — `[{}]` is OpenAPI for "this
    # operation needs no credentials", which is not the same as saying nothing.
    assert schema["paths"]["/api/v1/auth/register/"]["post"]["security"] == [{}]
    # The liveness probe carries no security requirement at all, having opted out with `auth=[]`.
    assert "security" not in schema["paths"]["/api/v1/health/"]["get"]


def test_money_is_documented_as_a_string() -> None:
    """ADR-0009's contract, visible in the schema a client generates from.

    Capable of failing if a serializer ever declares a money field as a float: the generated client
    would parse balances into IEEE doubles, losing the exactness the whole ledger is built on, and
    nothing else in the test suite would notice.
    """
    holding = _schema()["components"]["schemas"]["Holding"]["properties"]
    for field in ("cost_basis", "average_cost", "last_price", "market_value"):
        assert holding[field]["type"] == "string", f"{field} is not a string"


def test_the_docs_page_is_served(client: APIClient) -> None:
    assert client.get("/api/v1/schema/").status_code == 200
    assert client.get("/api/v1/docs/").status_code == 200
