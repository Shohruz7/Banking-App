"""The single error envelope every endpoint returns (ADR-0006).

Wraps DRF's default exception output so every non-2xx response has one shape:

    {"error": {"code": "<machine_code>", "message": "<human summary>", "details": {...}}}
"""

from typing import Any

from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


def api_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    response = drf_exception_handler(exc, context)
    if response is None:
        # Non-API exception: let Django's 500 handling take it.
        return None

    data = response.data
    if isinstance(data, dict) and "detail" in data:
        # Single-error responses (404, 401, 403, throttled, ...): DRF puts an
        # ErrorDetail under "detail"; its .code is the machine-readable code.
        detail = data["detail"]
        code = getattr(detail, "code", None) or data.get("code", "error")
        envelope = {"code": str(code), "message": str(detail), "details": {}}
    else:
        # Validation errors: keep the per-field breakdown as details.
        envelope = {"code": "validation_error", "message": "Invalid input.", "details": data}

    response.data = {"error": envelope}
    return response
