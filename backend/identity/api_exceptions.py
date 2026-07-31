"""Identity errors translated to HTTP, each with the machine code clients branch on (ADR-0006).

Same shape and reasoning as ``ledger.api_exceptions``: a client can tell "that code is wrong" from
"you never enrolled" without parsing prose.
"""

from rest_framework import status
from rest_framework.exceptions import APIException


class InvalidMFACode(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "That verification code is not valid."
    default_code = "invalid_mfa_code"


class MFAAlreadyEnrolled(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "This account already has a confirmed authenticator."
    default_code = "mfa_already_enrolled"


class MFANotEnrolled(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "This account has no authenticator to confirm or disable."
    default_code = "mfa_not_enrolled"
