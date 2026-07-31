"""Admin for identity state — read-only, with one deliberate exception.

Sessions and devices are never *edited* here; they are created and consumed by the auth flow. The
exception is the revoke action: an operator needs a way to kill a session, and routing it through
``revoke_session`` means it blacklists the token family and audits itself exactly like any other
revocation. Secrets are never displayed — a TOTP secret readable from the admin is a second factor
that anyone with staff access can clone.
"""

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from .models import AuthSession, MfaDevice, RevokeReason
from .services import revoke_session


@admin.register(AuthSession)
class AuthSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "created_at", "last_used_at", "revoked_at", "revoke_reason", "ip")
    list_filter = ("revoke_reason", "created_at")
    search_fields = ("user__username", "ip")
    readonly_fields = tuple(field.name for field in AuthSession._meta.fields)
    actions = ("revoke_selected_sessions",)

    def get_queryset(self, request: HttpRequest) -> QuerySet[AuthSession]:
        return super().get_queryset(request).select_related("user")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: AuthSession | None = None) -> bool:
        return False

    @admin.action(description="Revoke selected sessions")
    def revoke_selected_sessions(
        self, request: HttpRequest, queryset: QuerySet[AuthSession]
    ) -> None:
        revoked = sum(
            revoke_session(str(pk), reason=RevokeReason.ADMIN) is not None
            for pk in queryset.filter(revoked_at__isnull=True).values_list("pk", flat=True)
        )
        self.message_user(request, f"Revoked {revoked} session(s).", messages.SUCCESS)


@admin.register(MfaDevice)
class MfaDeviceAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "name", "confirmed_at", "last_used_at", "created_at")
    list_filter = ("confirmed_at", "created_at")
    search_fields = ("user__username", "name")
    # `secret` is deliberately excluded from both lists: it is the second factor itself.
    readonly_fields = ("id", "user", "name", "confirmed_at", "last_used_at", "created_at")
    exclude = ("secret", "last_used_counter")

    def get_queryset(self, request: HttpRequest) -> QuerySet[MfaDevice]:
        return super().get_queryset(request).select_related("user")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: MfaDevice | None = None) -> bool:
        return False
