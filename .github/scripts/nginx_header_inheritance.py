"""Refuse an nginx location that silently drops the headers it looks like it sends.

**This is the gate that would have caught the worst bug in this project's history on the day it was
written, and it is four lines of nginx documentation turned into a check.**

nginx does not merge directives into a nested block. `proxy_set_header` and `add_header` are each
inherited from the enclosing block *only when the nested block declares none of its own*. Declare
one and every inherited directive of that family disappears, with no warning at parse time, no
error at runtime, and nothing in the access log.

The project has now been bitten by this twice, once per directive family:

  `proxy_set_header`  Every proxied location sets `Connection`, for keepalive or for the WebSocket
                      upgrade. So every one of them dropped the whole server-level set, and had
                      been doing so since the config was written. `Host` fell back to `$proxy_host`
                      — the upstream group name — which Django answered with DisallowedHost as
                      soon as ALLOWED_HOSTS stopped happening to contain it. `X-Forwarded-Proto`
                      never arrived, so SECURE_SSL_REDIRECT would have produced an infinite
                      redirect the first time TLS terminated upstream. `X-Forwarded-For` never
                      arrived, so ADR-0038's per-client throttling was one shared bucket for the
                      internet. `X-Request-ID` never arrived, so correlated logging logged `-`.

  `add_header`        `location = /index.html` sets Cache-Control, so it served the application's
                      own HTML with no Content-Security-Policy while every other path showed one.

Both were fixed by moving the directives into an include and repeating that include in each block
that needs it. Repetition is not redundancy there; it is the only thing that makes them apply. This
asserts the repetition, because the failure mode is silence and a reviewer cannot be the control.

    python .github/scripts/nginx_header_inheritance.py deploy/nginx/app.conf.template
"""

import pathlib
import re
import sys

#: (directive that kills inheritance, include that restores it, what breaks without it).
RULES = (
    ("proxy_set_header", "proxy-headers.conf", "forwarded request headers"),
    ("add_header", "security-headers.conf", "security response headers"),
)

#: A block that proxies needs the request headers even if it declares no `proxy_set_header` of its
#: own today, because the next person to add a `Connection` line will not know this rule.
PROXY_REQUIRES = ("proxy_pass", "proxy-headers.conf", "forwarded request headers")

BLOCK_START = re.compile(r"^\s*location\s+(?P<name>\S+(?:\s+\S+)?)\s*\{")


def blocks(text: str) -> list[tuple[str, str, int]]:
    """Return (name, body, line number) for every `location` block, nesting included.

    A hand-rolled brace walk rather than a parser dependency: the file is fifty lines of one
    well-known shape, and a check that exists to prevent a silent misconfiguration should not
    itself be able to fail on a missing package.
    """
    found: list[tuple[str, str, int]] = []
    lines = text.splitlines()

    for index, line in enumerate(lines):
        match = BLOCK_START.match(line)
        if not match:
            continue

        depth = 0
        body: list[str] = []
        for following in lines[index:]:
            depth += following.count("{") - following.count("}")
            body.append(following)
            if depth == 0:
                break
        found.append((match.group("name"), "\n".join(body), index + 1))

    return found


def strip_comments(text: str) -> str:
    """A rule named only in a comment is not applied by nginx and must not satisfy the check."""
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def main(paths: list[str]) -> int:
    failures: list[str] = []

    for path in paths:
        source = strip_comments(pathlib.Path(path).read_text())

        for name, body, line in blocks(source):
            for directive, include, what in (*RULES, PROXY_REQUIRES):
                # The opening `location ... {` line itself cannot hold a directive, and the closing
                # brace cannot either; searching the whole body is fine because a nested block that
                # declares one is equally in violation.
                if directive not in body:
                    continue
                if include in body:
                    continue
                failures.append(
                    f"{path}:{line}: `location {name}` declares `{directive}` but does not "
                    f"`include /etc/nginx/{include};` — nginx will drop every inherited "
                    f"{what} in this block, silently."
                )

    for failure in sorted(set(failures)):
        print(failure, file=sys.stderr)

    if failures:
        print(
            "\nnginx inherits proxy_set_header and add_header into a nested block only when that "
            "block declares none of its own. Repeat the include; see the file's own comment.",
            file=sys.stderr,
        )
        return 1

    print(f"every location that needs the header includes has them ({len(paths)} file(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["deploy/nginx/app.conf.template"]))
