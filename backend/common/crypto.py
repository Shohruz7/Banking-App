"""Envelope encryption for sensitive columns, behind a swappable key seam (ADR-0027).

Two things are encrypted at rest as of Week 7: a user's TOTP secret, without which a database dump
is a complete MFA bypass for every enrolled user, and their account number.

**Envelope, not direct encryption.** Each value gets a fresh 256-bit *data key*; the data key is
what encrypts the plaintext, and the data key itself is encrypted (*wrapped*) by a long-lived *key
encryption key*. The indirection buys two things worth the extra 60 bytes per row: rotating the KEK
re-wraps data keys instead of re-encrypting every value, and the KEK never has to touch the
plaintext — which is precisely what makes a cloud KMS droppable in later, since a KMS will wrap a
data key for you but will not encrypt your rows.

**Where the KEK lives, in v1.** In the environment, as ``FIELD_ENCRYPTION_KEYS``. That is honestly
weaker than a KMS: anything that can read the process environment can decrypt the column, so this
defends against a stolen dump, a leaked backup and a replica, not against a compromised app server.
It is the right v1 trade — the seam is :class:`KeyProvider`, and a ``KmsKeyProvider`` implementing
two methods is the whole of the upgrade, with no model, migration or call site changing.

**Rotation.** Keys are held as an ordered map of *label* → key, and every ciphertext records the
label of the KEK that wrapped it. New writes use the first entry; reads accept any known label. So
rotating is: add a new key at the front, deploy, re-save the rows at leisure, drop the old key when
nothing references it. No flag day, and no window where half the table is unreadable.

Ciphertext layout, all one URL-safe base64 blob so it lives in a ``CharField``::

    v1 : <kek label> : b64(wrapped data key) : b64(nonce) : b64(ciphertext+tag)

The version prefix is the same discipline as ``ledger.fingerprints``: a change in construction gets
a new prefix rather than silently producing values the old reader mis-parses.
"""

import base64
import os
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

#: Bump when the layout below changes. Old ciphertexts keep their own prefix and stay readable.
SCHEME_VERSION = "v1"

#: AES-GCM standard nonce length. 96 bits is the size the mode is designed around; longer nonces get
#: hashed down internally and buy nothing.
_NONCE_BYTES = 12

_KEY_BYTES = 32


class DecryptionError(Exception):
    """A ciphertext could not be read: unknown key label, wrong key, or tampering.

    Deliberately one exception for all three. Distinguishing "wrong key" from "corrupted data" tells
    an attacker which of the two they achieved, and there is nothing the application would do
    differently.
    """


class KeyProvider(Protocol):
    """Where key encryption keys come from.

    The whole of the KMS seam. A ``KmsKeyProvider`` would implement these two methods against
    ``kms:GenerateDataKey`` and ``kms:Decrypt`` and nothing else in the codebase would change.
    """

    def current(self) -> tuple[str, bytes]:
        """The label and bytes of the KEK new writes should use."""
        ...

    def get(self, label: str) -> bytes:
        """The KEK bytes for a label written earlier. Raises ``KeyError`` if it is unknown."""
        ...


class SettingsKeyProvider:
    """KEKs from ``settings.FIELD_ENCRYPTION_KEYS`` — an ordered ``{label: urlsafe-b64 key}`` map.

    The first entry is current. Ordering is load-bearing and Python dicts preserve it, which is why
    this is a dict and not a set of environment variables discovered by prefix.
    """

    def __init__(self, keys: dict[str, str] | None = None) -> None:
        self._raw = keys if keys is not None else getattr(settings, "FIELD_ENCRYPTION_KEYS", {})
        if not self._raw:
            raise ImproperlyConfigured(
                "FIELD_ENCRYPTION_KEYS is empty; sensitive columns cannot be read or written. "
                'Generate one with `python -c "import base64,os; '
                'print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`.'
            )

    def _decode(self, label: str) -> bytes:
        key = base64.urlsafe_b64decode(self._raw[label])
        if len(key) != _KEY_BYTES:
            raise ImproperlyConfigured(
                f"Field encryption key {label!r} is {len(key)} bytes; AES-256 needs {_KEY_BYTES}."
            )
        return key

    def current(self) -> tuple[str, bytes]:
        label = next(iter(self._raw))
        return label, self._decode(label)

    def get(self, label: str) -> bytes:
        return self._decode(label)


def _provider() -> KeyProvider:
    """Resolved per call rather than at import.

    Tests override ``FIELD_ENCRYPTION_KEYS`` with the ``settings`` fixture, and a provider captured
    at import time would ignore them — the same reason DRF reads throttle rates at call time.
    """
    return SettingsKeyProvider()


def encrypt(plaintext: str) -> str:
    """Encrypt a value under a fresh data key, wrapped by the current KEK.

    The empty string is passed through unchanged. A blank column is the *absence* of a secret rather
    than a secret whose value is blank, and encrypting it would make "no account number yet"
    indistinguishable from a real one at a glance — while also making every NULL-ish row unique
    ciphertext that a reader has to decrypt to discover means nothing.
    """
    if plaintext == "":
        return ""

    label, kek = _provider().current()
    data_key = AESGCM.generate_key(bit_length=256)

    data_nonce = os.urandom(_NONCE_BYTES)
    body = AESGCM(data_key).encrypt(data_nonce, plaintext.encode(), None)

    wrap_nonce = os.urandom(_NONCE_BYTES)
    wrapped = AESGCM(kek).encrypt(wrap_nonce, data_key, None)

    return ":".join(
        [
            SCHEME_VERSION,
            label,
            _b64(wrap_nonce + wrapped),
            _b64(data_nonce),
            _b64(body),
        ]
    )


def decrypt(ciphertext: str) -> str:
    """Recover a value written by :func:`encrypt`, or raise :class:`DecryptionError`."""
    if ciphertext == "":
        return ""

    try:
        version, label, wrapped_blob, data_nonce_b64, body_b64 = ciphertext.split(":")
    except ValueError as exc:
        raise DecryptionError("Ciphertext is not in the expected format.") from exc

    if version != SCHEME_VERSION:
        raise DecryptionError(f"Unknown ciphertext scheme {version!r}.")

    try:
        kek = _provider().get(label)
    except KeyError as exc:
        raise DecryptionError(f"No encryption key labelled {label!r} is configured.") from exc

    try:
        wrapped = _unb64(wrapped_blob)
        data_key = AESGCM(kek).decrypt(wrapped[:_NONCE_BYTES], wrapped[_NONCE_BYTES:], None)
        return AESGCM(data_key).decrypt(_unb64(data_nonce_b64), _unb64(body_b64), None).decode()
    except (InvalidTag, ValueError) as exc:
        raise DecryptionError("Ciphertext failed authentication.") from exc


def is_encrypted(value: str) -> bool:
    """Whether a stored value has already been through :func:`encrypt`.

    Used by the backfill migrations so a re-run is a no-op, and by nothing else — application code
    should never be in doubt about which side of the boundary it is on.
    """
    return value.startswith(f"{SCHEME_VERSION}:")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode()


def _unb64(encoded: str) -> bytes:
    return base64.urlsafe_b64decode(encoded)
