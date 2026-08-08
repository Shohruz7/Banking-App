"""Customer-facing account numbers (ADR-0027).

Until Week 7 the only identifier an account had was its UUIDv7 primary key. That works as an API
address and is wrong as a *label*: it is 36 characters nobody can read down a phone line, it is the
internal key welded to the public contract, and being time-ordered it leaks when the account was
opened (ADR-0005's accepted risk, which was fine while the id was the only option).

So an account now also carries a short number with a check digit. The PK stays the addressing
mechanism — routes, foreign keys and the socket all keep using it — and this is a label a human
reads. Keeping that boundary is what stops the number from becoming a URL migration later.

The check digit is ISO 7064 MOD 97-10, the scheme IBAN uses: it catches every single-digit error and
every transposition of adjacent digits, which are the two mistakes people actually make when copying
a number. It is *not* a security control — the number is guessable by construction and authorization
never depends on it.
"""

import secrets

#: Digits in the body, before the two check digits. Ten gives 10^10 accounts before the space feels
#: crowded, and reads as three comfortable groups.
_BODY_DIGITS = 10

PREFIX = "BK"


def _check_digits(body: str) -> str:
    """ISO 7064 MOD 97-10 over the body, as two digits."""
    return f"{98 - (int(body + '00') % 97):02d}"


def generate() -> str:
    """A fresh account number, formatted ``BK-##########-##``.

    Random rather than sequential: a sequential number tells every customer how many accounts the
    bank has and how fast it is growing, and lets one customer guess another's. ``secrets`` rather
    than ``random`` for the same reason the choice is worth making at all.
    """
    body = "".join(secrets.choice("0123456789") for _ in range(_BODY_DIGITS))
    return f"{PREFIX}-{body}-{_check_digits(body)}"


def is_valid(number: str) -> bool:
    """Whether a number is well-formed and its check digits agree with its body."""
    try:
        prefix, body, check = number.split("-")
    except ValueError:
        return False
    if prefix != PREFIX or len(body) != _BODY_DIGITS or not body.isdigit():
        return False
    return check == _check_digits(body)


def mask(number: str) -> str:
    """``BK-••••••6789`` — enough to recognise an account, not enough to quote it.

    What list views and statements show. The full number is available on the detail endpoint, to its
    owner, which is the only place it is worth decrypting.
    """
    if not number:
        return ""
    return f"{PREFIX}-{'•' * 6}{number[-7:-3]}"


def last4(number: str) -> str:
    """The four digits kept in plaintext so a masked label costs no decryption."""
    return number[-7:-3] if number else ""
