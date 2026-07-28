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
