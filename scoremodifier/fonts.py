"""Brand fonts for generated PDFs: Raleway (SIL OFL 1.1), embedded as TTF.

The faces live in ``assets/`` next to the logo. PyMuPDF's built-in
``fitz.get_text_length`` only knows the base-14 fonts, so measurement goes
through cached :class:`fitz.Font` instances instead; drawing registers the
face on each page under the same reserved fontname.
"""

from __future__ import annotations

from pathlib import Path

import fitz  # PyMuPDF

_ASSETS = Path(__file__).with_name("assets")

RALEWAY_REGULAR_FILE = _ASSETS / "Raleway-Regular.ttf"
RALEWAY_BOLD_FILE = _ASSETS / "Raleway-Bold.ttf"

# page-level fontnames used with insert_text / insert_textbox
F_REG = "ralw"  # Raleway Regular (body)
F_BOLD = "ralwbd"  # Raleway Bold (display / headings)

_FILES: dict[str, Path] = {F_REG: RALEWAY_REGULAR_FILE, F_BOLD: RALEWAY_BOLD_FILE}
_measure: dict[str, fitz.Font] = {}


def register_fonts(page: "fitz.Page") -> None:
    """Embed both Raleway faces on ``page`` under :data:`F_REG` / :data:`F_BOLD`."""
    for name, path in _FILES.items():
        page.insert_font(fontname=name, fontfile=str(path))


def text_length(text: str, fontname: str, fontsize: float) -> float:
    """Width of ``text`` in points — replacement for ``fitz.get_text_length``."""
    font = _measure.get(fontname)
    if font is None:
        font = _measure[fontname] = fitz.Font(fontfile=str(_FILES[fontname]))
    return font.text_length(text, fontsize=fontsize)
