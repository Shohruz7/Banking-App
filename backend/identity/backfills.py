"""One-time data moves for the identity app, kept out of ``migrations/`` so coverage sees them."""

from typing import Any

from common.crypto import encrypt, is_encrypted


def encrypt_mfa_secrets(apps: Any) -> int:
    """Move every stored TOTP secret from plaintext into envelope ciphertext (ADR-0027).

    Idempotent via :func:`common.crypto.is_encrypted`, so a re-run after a partial failure encrypts
    only what is left rather than double-wrapping what succeeded — which would be unrecoverable,
    since the inner layer would decrypt to a base32 string that is not the secret.

    Returns the number of rows converted.
    """
    MfaDevice = apps.get_model("identity", "MfaDevice")

    # The plaintext column only exists in the historical state this migration runs against — it is
    # dropped in the very next operation. Asked of the *current* registry, or re-run after the
    # migration has completed, there is by construction nothing left to convert.
    field_names = {field.name for field in MfaDevice._meta.get_fields()}
    if "secret" not in field_names:
        return 0

    converted = 0
    for device in MfaDevice.objects.exclude(secret=""):
        if is_encrypted(device.secret_ciphertext):
            continue
        device.secret_ciphertext = encrypt(device.secret)
        device.save(update_fields=["secret_ciphertext"])
        converted += 1
    return converted
