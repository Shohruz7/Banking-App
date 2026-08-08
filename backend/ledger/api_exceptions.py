"""Domain errors translated to HTTP, each with the machine code clients branch on (ADR-0006).

``common.exceptions.api_exception_handler`` reads ``default_code`` off these and emits
``{"error": {"code": ..., "message": ..., "details": {}}}``. Keeping them here — rather than
raising bare ``ValidationError`` — means a client can distinguish "you're broke" from "that
account doesn't exist" without parsing prose.
"""

from rest_framework import status
from rest_framework.exceptions import APIException


class InsufficientFunds(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "The source account does not have enough funds for this transfer."
    default_code = "insufficient_funds"


class SameAccount(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "Source and destination must be different accounts."
    default_code = "same_account"


class DestinationNotFound(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "The destination account does not exist."
    default_code = "destination_not_found"


class InvalidTransfer(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "The transfer request is not valid."
    default_code = "invalid_transfer"


class IdempotencyKeyConflict(APIException):
    # 409, following OrderNotOpen's precedent: the request is well-formed, and the conflict is with
    # state rather than with the body. Retrying it unchanged will not help, which is the distinction
    # a 400 would blur.
    #
    # The detail deliberately says nothing about the request the key was first used for. That
    # request may belong to somebody else — the key column is unique across the whole table — and
    # echoing it back would replace the disclosure this constraint exists to close (ADR-0024).
    status_code = status.HTTP_409_CONFLICT
    default_detail = "That idempotency key was already used for a different request."
    default_code = "idempotency_key_conflict"
