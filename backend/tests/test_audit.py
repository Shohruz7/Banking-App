"""The append-only audit log, and the transaction placement that makes it honest.

Two tests here are the ones worth writing first and watching fail:
``test_audit_row_vanishes_when_the_transfer_rolls_back`` (a posting row must not survive a
transaction that aborted) and ``test_rejected_transfer_is_audited_outside_the_transaction`` (a
rejection row must survive the transaction that raised). Get the placement backwards and both
failures are *silent* — the log simply says the wrong thing.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.db import Error as DatabaseError
from django.db import transaction
from django.urls import reverse
from rest_framework.test import APIClient

from audit.context import audit_context
from audit.models import AuditAction, AuditEvent
from audit.services import record_audit
from ledger import services as ledger_services
from ledger.models import JournalEntry
from ledger.services import transfer

from .conftest import obtain_tokens, totp_now
from .factories import AccountFactory, fund_account

pytestmark = pytest.mark.django_db


def test_audit_row_written_on_successful_transfer(
    auth_client: APIClient, password_user: User
) -> None:
    source = AccountFactory.create(owner=password_user)
    destination = AccountFactory.create(owner=password_user)
    fund_account(source, Decimal("100.0000"))

    response = auth_client.post(
        reverse("transfer-create"),
        {
            "source_account": str(source.pk),
            "destination_account": str(destination.pk),
            "amount": "30.0000",
        },
        format="json",
    )
    assert response.status_code == 201

    event = AuditEvent.objects.get(action=AuditAction.TRANSFER_POSTED)
    assert event.actor == password_user
    assert event.actor_label == password_user.username
    assert event.target_type == "journal_entry"
    assert event.target_id == response.json()["id"]
    # Money is a string in the log exactly as it is on the wire (ADR-0009), never a float.
    assert event.context["amount"] == "30.0000"
    assert isinstance(event.context["amount"], str)
    assert event.context["source_account"] == str(source.pk)
    # Transport facts arrive from the ambient context, not from an argument to transfer().
    assert event.ip == "127.0.0.1"
    assert event.request_id


def test_audit_row_vanishes_when_the_transfer_rolls_back(
    password_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The posting row lives *inside* the transaction, and that is the whole point.

    If the transaction aborts, the money did not move — and a surviving audit row would be a lie.

    The sabotage has to happen *after* the row is written and *before* commit, or the test cannot
    tell the two placements apart: a failure earlier than ``record_audit`` leaves no row either
    way. So the audit call is wrapped to succeed and then explode. With the call inside the atomic
    block both the entry and the row roll back; move it after the block and both survive.
    """
    source = AccountFactory.create(owner=password_user)
    destination = AccountFactory.create(owner=password_user)
    fund_account(source, Decimal("100.0000"))
    entries_before = JournalEntry.objects.count()

    real_record_audit = ledger_services.record_audit

    def record_then_fail(**kwargs: object) -> object:
        real_record_audit(**kwargs)  # type: ignore[arg-type]
        raise RuntimeError("commit failed after the audit row was written")

    monkeypatch.setattr(ledger_services, "record_audit", record_then_fail)

    with pytest.raises(RuntimeError):
        transfer(
            source=source,
            destination=destination,
            amount=Decimal("30.0000"),
            actor=password_user,
        )

    # Neither survives. The log agrees with the ledger by construction, not by discipline.
    assert not AuditEvent.objects.filter(action=AuditAction.TRANSFER_POSTED).exists()
    assert JournalEntry.objects.count() == entries_before


def test_rejected_transfer_is_audited_outside_the_transaction(
    auth_client: APIClient, password_user: User
) -> None:
    """The mirror case.

    ``InsufficientFundsError`` is raised *inside* ``transfer``'s atomic block, so the rejection row
    is written by the view after the block has unwound. Written at the raise site instead, it would
    be rolled back by the very exception it records — and nothing would notice.
    """
    source = AccountFactory.create(owner=password_user)
    destination = AccountFactory.create(owner=password_user)
    fund_account(source, Decimal("10.0000"))

    response = auth_client.post(
        reverse("transfer-create"),
        {
            "source_account": str(source.pk),
            "destination_account": str(destination.pk),
            "amount": "50.0000",
        },
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "insufficient_funds"

    event = AuditEvent.objects.get(action=AuditAction.TRANSFER_REJECTED)
    assert event.actor == password_user
    assert event.context["reason"] == "insufficient_funds"
    assert event.context["amount"] == "50.0000"


def test_idempotent_replay_is_audited_as_a_replay(
    auth_client: APIClient, password_user: User
) -> None:
    source = AccountFactory.create(owner=password_user)
    destination = AccountFactory.create(owner=password_user)
    fund_account(source, Decimal("100.0000"))
    payload = {
        "source_account": str(source.pk),
        "destination_account": str(destination.pk),
        "amount": "30.0000",
        "idempotency_key": "key-1",
    }

    assert auth_client.post(reverse("transfer-create"), payload, format="json").status_code == 201
    assert auth_client.post(reverse("transfer-create"), payload, format="json").status_code == 200

    assert AuditEvent.objects.filter(action=AuditAction.TRANSFER_POSTED).count() == 1
    assert AuditEvent.objects.filter(action=AuditAction.TRANSFER_REPLAYED).count() == 1


def test_audit_rows_on_login_logout_and_failed_login(
    api_client: APIClient, password_user: User
) -> None:
    bad = api_client.post(
        reverse("token_obtain_pair"),
        {"username": password_user.username, "password": "wrong"},
        format="json",
    )
    assert bad.status_code == 401

    failure = AuditEvent.objects.get(action=AuditAction.LOGIN_FAILED)
    # No actor: the credentials were never proven. The attempted identifier is still recorded.
    assert failure.actor is None
    assert failure.actor_label == password_user.username

    tokens = obtain_tokens(api_client, password_user)
    success = AuditEvent.objects.get(action=AuditAction.LOGIN_SUCCEEDED)
    assert success.actor == password_user
    assert success.context == {"mfa": False}

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    client.post(reverse("logout"), {"refresh": tokens["refresh"]}, format="json")

    assert AuditEvent.objects.filter(action=AuditAction.LOGOUT).exists()
    revoked = AuditEvent.objects.get(action=AuditAction.SESSION_REVOKED)
    assert revoked.context["reason"] == "logout"


def test_audit_rows_on_the_mfa_journey(
    api_client: APIClient, auth_client: APIClient, password_user: User
) -> None:
    secret = auth_client.post(reverse("mfa-enroll")).json()["secret"]
    assert AuditEvent.objects.filter(action=AuditAction.MFA_ENROLLED).exists()

    auth_client.post(reverse("mfa-confirm"), {"code": totp_now(secret)}, format="json")
    assert AuditEvent.objects.filter(action=AuditAction.MFA_CONFIRMED).exists()

    challenge = obtain_tokens(api_client, password_user)
    assert AuditEvent.objects.filter(action=AuditAction.MFA_CHALLENGED).exists()

    api_client.post(
        reverse("token_mfa_verify"),
        {"mfa_token": challenge["mfa_token"], "code": "000000"},
        format="json",
    )
    assert AuditEvent.objects.filter(action=AuditAction.MFA_FAILED).exists()


def test_audit_row_on_reuse_detection(api_client: APIClient, password_user: User) -> None:
    tokens = obtain_tokens(api_client, password_user)
    rotated = api_client.post(
        reverse("token_refresh"), {"refresh": tokens["refresh"]}, format="json"
    ).json()

    api_client.post(reverse("token_refresh"), {"refresh": tokens["refresh"]}, format="json")

    event = AuditEvent.objects.get(action=AuditAction.TOKEN_REUSE_DETECTED)
    assert event.actor == password_user
    assert event.target_type == "auth_session"

    revoked = AuditEvent.objects.get(action=AuditAction.SESSION_REVOKED)
    assert revoked.context["reason"] == "reuse_detected"

    # Every surviving token in the family is now blacklisted too, so each one that arrives would
    # look like a fresh replay. Exactly one detection is recorded — the consequences of the breach
    # must not bury the breach.
    api_client.post(reverse("token_refresh"), {"refresh": rotated["refresh"]}, format="json")
    api_client.post(reverse("token_refresh"), {"refresh": tokens["refresh"]}, format="json")

    assert AuditEvent.objects.filter(action=AuditAction.TOKEN_REUSE_DETECTED).count() == 1
    assert AuditEvent.objects.filter(action=AuditAction.SESSION_REVOKED).count() == 1


def test_audit_events_cannot_be_updated(password_user: User) -> None:
    """Append-only, enforced by Postgres — not by the ORM, and not by convention."""
    event = record_audit(action=AuditAction.LOGIN_SUCCEEDED, actor=password_user)

    # A failed statement poisons the surrounding transaction, so each attempt is isolated — the
    # same lesson transfer()'s IntegrityError recovery already encodes.
    with pytest.raises(DatabaseError) as exc_info, transaction.atomic():
        AuditEvent.objects.filter(pk=event.pk).update(action=AuditAction.LOGIN_FAILED)
    assert "append-only" in str(exc_info.value).lower()

    event.refresh_from_db()
    assert event.action == AuditAction.LOGIN_SUCCEEDED


def test_audit_events_cannot_be_deleted(password_user: User) -> None:
    event = record_audit(action=AuditAction.LOGIN_SUCCEEDED, actor=password_user)

    with pytest.raises(DatabaseError) as exc_info, transaction.atomic():
        AuditEvent.objects.filter(pk=event.pk).delete()
    assert "append-only" in str(exc_info.value).lower()

    assert AuditEvent.objects.filter(pk=event.pk).exists()


def test_model_level_guards_fail_fast(password_user: User) -> None:
    """Belt to the trigger's braces: a clear Python error instead of an opaque InternalError."""
    event = record_audit(action=AuditAction.LOGIN_SUCCEEDED, actor=password_user)

    with pytest.raises(ValueError, match="append-only"):
        event.save()
    with pytest.raises(ValueError, match="append-only"):
        event.delete()


def test_audit_context_never_contains_secrets(
    api_client: APIClient, auth_client: APIClient, password_user: User
) -> None:
    """An audit log that records the second factor is worse than no audit log."""
    secret = auth_client.post(reverse("mfa-enroll")).json()["secret"]
    code = totp_now(secret)
    auth_client.post(reverse("mfa-confirm"), {"code": code}, format="json")

    challenge = obtain_tokens(api_client, password_user)
    api_client.post(
        reverse("token_mfa_verify"),
        {"mfa_token": challenge["mfa_token"], "code": totp_now(secret)},
        format="json",
    )

    forbidden = (secret, code, "sw0rdf1sh-test-pw", challenge["mfa_token"])
    for event in AuditEvent.objects.all():
        blob = str(event.context)
        for needle in forbidden:
            assert needle not in blob, f"{event.action} leaked a credential"


def test_record_audit_scrubs_credential_shaped_keys(password_user: User) -> None:
    event = record_audit(
        action=AuditAction.LOGIN_SUCCEEDED,
        actor=password_user,
        context={
            "password": "hunter2",
            "nested": {"otp_code": "123456", "amount": "10.0000"},
            "amount": "10.0000",
        },
    )

    assert event.context["password"] == "[redacted]"
    assert event.context["nested"]["otp_code"] == "[redacted]"
    # Everything that is not credential-shaped survives untouched.
    assert event.context["amount"] == "10.0000"
    assert event.context["nested"]["amount"] == "10.0000"


def test_audit_works_without_a_request(password_user: User) -> None:
    """Week 5 Celery tasks and Week 6 Channels consumers write rows the same way.

    With ambient context the transport facts are captured; with none, the row is still written —
    the absence of a request must never be an error.
    """
    source = AccountFactory.create(owner=password_user)
    destination = AccountFactory.create(owner=password_user)
    fund_account(source, Decimal("100.0000"))

    with audit_context(actor=password_user, ip="10.0.0.9", request_id="req-abc"):
        transfer(
            source=source,
            destination=destination,
            amount=Decimal("5.0000"),
            actor=password_user,
        )

    event = AuditEvent.objects.filter(action=AuditAction.TRANSFER_POSTED).first()
    assert event is not None
    assert event.actor == password_user
    assert event.ip == "10.0.0.9"
    assert event.request_id == "req-abc"

    # And with no ambient context at all.
    transfer(
        source=source,
        destination=destination,
        amount=Decimal("5.0000"),
        actor=password_user,
    )
    bare = AuditEvent.objects.filter(action=AuditAction.TRANSFER_POSTED).first()
    assert bare is not None
    assert bare.ip is None


def test_explicit_actor_beats_the_ambient_one(password_user: User) -> None:
    other = User.objects.create_user("someone-else")

    with audit_context(actor=password_user):
        event = record_audit(action=AuditAction.LOGIN_SUCCEEDED, actor=other)

    assert event.actor == other
    assert event.actor_label == "someone-else"
