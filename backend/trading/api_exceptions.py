"""Trading domain errors translated to HTTP, each with the machine code clients branch on.

Same contract as ``ledger.api_exceptions``: ``common.exceptions.api_exception_handler`` reads
``default_code`` off these and emits the ADR-0006 envelope.
"""

from rest_framework import status
from rest_framework.exceptions import APIException


class InsufficientShares(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "You do not hold enough shares to sell that quantity."
    default_code = "insufficient_shares"


class InvalidOrder(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "The order request is not valid."
    default_code = "invalid_order"


class InstrumentNotFound(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "No instrument with that symbol exists."
    default_code = "instrument_not_found"


class InstrumentInactive(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "That instrument is no longer tradeable."
    default_code = "instrument_inactive"


class OrderNotOpen(APIException):
    # 409, not 400: the request is well-formed and would have been valid a moment ago. The client
    # is racing its own order's resolution, which is a state conflict rather than a bad request.
    status_code = status.HTTP_409_CONFLICT
    default_detail = "That order is no longer open."
    default_code = "order_not_open"


class OrderKeyConflict(APIException):
    # Same 409 reasoning, and the same silence about the original request (ADR-0024).
    status_code = status.HTTP_409_CONFLICT
    default_detail = "That idempotency key was already used for a different order."
    default_code = "order_key_conflict"
