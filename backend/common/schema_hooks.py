"""Preprocessing hooks for schema generation (ADR-0028)."""

from typing import Any


def register_extensions(endpoints: list[Any], **kwargs: Any) -> list[Any]:
    """Import ``common.schema`` so its authentication extension registers itself.

    drf-spectacular discovers extensions by import side effect, and nothing else in the project
    imports ``common.schema`` — so without this hook the class exists and is never consulted, and
    every authenticated endpoint is documented as requiring no credentials at all.
    """
    import common.schema  # noqa: F401 — imported for the registration side effect

    return endpoints
