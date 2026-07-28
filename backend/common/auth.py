"""Type-narrowing helper for authenticated request users."""

from typing import cast

from django.contrib.auth.models import User
from rest_framework.request import Request


def request_user(request: Request) -> User:
    """Return the authenticated user behind an ``IsAuthenticated`` view.

    DRF types ``request.user`` as ``User | AnonymousUser`` because it cannot see which permission
    classes guard the view. Callers of this helper all sit behind ``IsAuthenticated``, so the
    anonymous case is already answered with a 401 before any view body runs — this narrows the
    type without pretending to be a second access check.
    """
    return cast(User, request.user)
