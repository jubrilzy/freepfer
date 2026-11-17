"""
PDF Editor Core
Handles opening, scanning, editing, and saving PDFs with font fidelity.
"""

import pymupdf as fitz
from dataclasses import dataclass, field
from typing import Optional, List
import re


@dataclass
class TextSpan:
    text: str
    font_name: str
    font_size: float
    color: int        # packed RGB integer
    bbox: tuple       # (x0, y0, x1, y1)
    page_num: int
    block_idx: int
    line_idx: int
    span_idx: int
    flags: int        # bold=16, italic=2


@dataclass
class EditResult:
    success: bool
    font_preserved: bool
    font_used: str
    message: str = ""


class PDFEditor:
    def __init__(self, pdf_path: str):
        self.path = pdf_path
        self.doc = fitz.open(pdf_path)
        self._spans: List[TextSpan] = []
        self._scan()

    # ------------------------------------------------------------------ #
    #  Scanning                                                            #
    # ------------------------------------------------------------------ #

    def _scan(self):
        self._spans = []
        for page_num in range(len(self.doc)):
            page = self.doc[page_num]
            # Use "dict" (not "rawdict") — spans have a "text" key directly
            raw = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
            for b_idx, block in enumerate(raw.get("blocks", [])):
                if block.get("type") != 0:
                    continue
                for l_idx, line in enumerate(block.get("lines", [])):
                    for s_idx, span in enumerate(line.get("spans", [])):
                        text = span.get("text", "")
                        if text.strip():
                            self._spans.append(TextSpan(
                                text=text,
                                font_name=span.get("font", ""),
                                font_size=span.get("size", 12),
                                color=span.get("color", 0),
                                bbox=tuple(span["bbox"]),
                                page_num=page_num,
                                block_idx=b_idx,
                                line_idx=l_idx,
                                span_idx=s_idx,
                                flags=span.get("flags", 0),
                            ))

    def get_spans(self, page_num: Optional[int] = None) -> List[TextSpan]:
        if page_num is None:
            return list(self._spans)
        return [s for s in self._spans if s.page_num == page_num]

    def get_unique_fonts(self) -> dict:
        """Return {font_name: count} across the whole document."""
        counts = {}
        for s in self._spans:
            counts[s.font_name] = counts.get(s.font_name, 0) + 1
        return counts

    # ------------------------------------------------------------------ #
    #  Editing                                                             #
    # ------------------------------------------------------------------ #

    def replace_text(
        self,
        span: TextSpan,
        new_text: str,
        font_path: Optional[str] = None,
    ) -> EditResult:
        """
        Replace span text in-place.
        1. Redact the original rectangle.
        2. Sample background color at that region.
        3. Re-draw with matched font.
        Returns EditResult with fidelity info.
        """
        page = self.doc[span.page_num]
        rect = fitz.Rect(span.bbox)

        # --- sample background ---
        bg = self._sample_bg(page, rect)

        # --- redact ---
        annot = page.add_redact_annot(rect, fill=bg)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        # --- resolve font ---
        font_preserved = False
        font_used = "Helvetica (fallback)"

        # Map common PDF base font names to PyMuPDF built-ins
        BUILTIN_MAP = {
            "helvetica": "helv", "helvetica-bold": "hebo",
            "helvetica-oblique": "heit", "helvetica-boldoblique": "hebi",
            "times-roman": "tiro", "times-bold": "tibo",
            "times-italic": "tiit", "times-bolditalic": "tibi",
            "courier": "cour", "courier-bold": "cobo",
            "courier-oblique": "coit", "courier-boldoblique": "cobi",
            "symbol": "symb", "zapfdingbats": "zadb",
        }

        # Resolve insert_text kwargs: fontfile takes precedence over fontname
        insert_kwargs: dict = {}

        if font_path:
            insert_kwargs["fontfile"] = font_path
            font_used = font_path
            font_preserved = True
        else:
            clean_name = re.sub(r'^[A-Z]{6}\+', '', span.font_name)
            builtin_key = clean_name.lower().replace(" ", "-")
            if builtin_key in BUILTIN_MAP:
                insert_kwargs["fontname"] = BUILTIN_MAP[builtin_key]
                font_used = clean_name
                font_preserved = True
            else:
                # Pass the name and let PyMuPDF try; fall back to helv
                insert_kwargs["fontname"] = "helv"
                font_used = "Helvetica (fallback)"

        # --- unpack color ---
        r = ((span.color >> 16) & 0xFF) / 255
        g = ((span.color >> 8) & 0xFF) / 255
        b = (span.color & 0xFF) / 255

        # y baseline
        baseline_y = rect.y0 + span.font_size * 0.85

        try:
            page.insert_text(
                (rect.x0, baseline_y),
                new_text,
                fontsize=span.font_size,
                color=(r, g, b),
                **insert_kwargs,
            )
        except Exception as e:
            return EditResult(success=False, font_preserved=False,
                              font_used=font_used, message=str(e))

        self._scan()
        return EditResult(
            success=True,
            font_preserved=font_preserved,
            font_used=font_used,
        )

    def _sample_bg(self, page, rect: fitz.Rect):
        """Sample average background color in rect; fall back to white."""
        try:
            clip = rect + fitz.Rect(-2, -2, 2, 2)  # slight padding
            pix = page.get_pixmap(clip=clip, matrix=fitz.Matrix(1, 1))
            samples = pix.samples
            if len(samples) >= 3:
                r, g, b = samples[0] / 255, samples[1] / 255, samples[2] / 255
                return (r, g, b)
        except Exception:
            pass
        return (1, 1, 1)

    # ------------------------------------------------------------------ #
    #  Rendering                                                           #
    # ------------------------------------------------------------------ #

    def render_page(self, page_num: int, zoom: float = 1.5):
        """Return a fitz.Pixmap for the given page."""
        page = self.doc[page_num]
        mat = fitz.Matrix(zoom, zoom)
        return page.get_pixmap(matrix=mat, alpha=False)

    # ------------------------------------------------------------------ #
    #  Save                                                                #
    # ------------------------------------------------------------------ #

    def save(self, output_path: str):
        self.doc.save(output_path, garbage=4, deflate=True, clean=True)

    # ------------------------------------------------------------------ #
    #  Properties                                                          #
    # ------------------------------------------------------------------ #

    @property
    def page_count(self) -> int:
        return len(self.doc)

    @property
    def title(self) -> str:
        meta = self.doc.metadata
        return meta.get("title", "") or self.path
