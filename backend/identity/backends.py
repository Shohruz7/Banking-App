"""Authentication backend that accepts an email address in the username field.

ADR-0011 keeps ``django.contrib.auth.models.User`` rather than swapping ``AUTH_USER_MODEL``, so
email login is bought here instead of with a migration. The wire field stays named ``username``
(that is what ``TokenObtainSerializer`` derives from ``USERNAME_FIELD``); it simply accepts either.
"""

from typing import Any

from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import User
from django.db.models import Q
from django.http import HttpRequest


class EmailOrUsernameBackend(ModelBackend):
    """Resolve the identifier against ``username`` or ``email``, case-insensitively."""

    def authenticate(
        self,
        request: HttpRequest | None,
        username: str | None = None,
        password: str | None = None,
        **kwargs: Any,
    ) -> User | None:
        if username is None or password is None:
            return None

        # `.first()` on an ordered-by-pk queryset, not `.get()`: User.email carries no unique
        # constraint, so a legacy duplicate must not raise MultipleObjectsReturned on every login.
        # Registration blocks new collisions (RegisterSerializer), which makes this belt-and-braces.
        user = (
            User.objects.filter(Q(username__iexact=username) | Q(email__iexact=username))
            .order_by("pk")
            .first()
        )
        if user is None:
            # Hash anyway so a missing user costs the same wall-clock time as a wrong password.
            User().set_password(password)
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
