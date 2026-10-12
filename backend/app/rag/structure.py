"""Structure carried from extraction into chunking; no external services."""

from dataclasses import dataclass


@dataclass
class ExtractUnit:
    text: str
    section: tuple[str, ...] = ()
    kind: str = "paragraph"
    page: int | None = None
