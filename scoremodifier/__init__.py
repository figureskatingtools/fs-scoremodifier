"""Score modifier tools for figure skating result PDFs."""

from .per_skater import (
    DEFAULT_FOOTER_TEXT,
    NotPerSkaterReport,
    split_per_skater,
)

__all__ = ["split_per_skater", "DEFAULT_FOOTER_TEXT", "NotPerSkaterReport"]
