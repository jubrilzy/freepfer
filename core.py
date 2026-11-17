"""
PDF Editor Core
Handles opening, scanning, editing, moving, and image operations on PDFs.
"""

import pymupdf as fitz
from dataclasses import dataclass
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
class ImageBlock:
    bbox: tuple       # (x0, y0, x1, y1)
    width: int
    height: int
    xref: int
    ext: str          # png, jpeg, etc.
    page_num: int


@dataclass
class EditResult:
    success: bool
    font_preserved: bool
    font_used: str
    message: str = ""


BUILTIN_MAP = {
    "helvetica": "helv", "helvetica-bold": "hebo",
    "helvetica-oblique": "heit", "helvetica-boldoblique": "hebi",
    "times-roman": "tiro", "times-bold": "tibo",
    "times-italic": "tiit", "times-bolditalic": "tibi",
    "courier": "cour", "courier-bold": "cobo",
    "courier-oblique": "coit", "courier-boldoblique": "cobi",
    "symbol": "symb", "zapfdingbats": "zadb",
}


class PDFEditor:
    def __init__(self, pdf_path: str):
        self.path = pdf_path
        self.doc = fitz.open(pdf_path)
        self._spans: List[TextSpan] = []
        self._images: List[ImageBlock] = []
        self._scan()

    # ------------------------------------------------------------------ #
    #  Scanning                                                            #
    # ------------------------------------------------------------------ #

    def _scan(self):
        self._spans = []
        self._images = []
        for page_num in range(len(self.doc)):
            page = self.doc[page_num]
            raw = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
            for b_idx, block in enumerate(raw.get("blocks", [])):
                btype = block.get("type")
                if btype == 0:   # text block
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
                elif btype == 1:  # image block
                    self._images.append(ImageBlock(
                        bbox=tuple(block["bbox"]),
                        width=block.get("width", 0),
                        height=block.get("height", 0),
                        xref=block.get("xref", 0),
                        ext=block.get("ext", "png"),
                        page_num=page_num,
                    ))

    def get_spans(self, page_num: Optional[int] = None) -> List[TextSpan]:
        if page_num is None:
            return list(self._spans)
        return [s for s in self._spans if s.page_num == page_num]

    def get_images(self, page_num: Optional[int] = None) -> List[ImageBlock]:
        if page_num is None:
            return list(self._images)
        return [i for i in self._images if i.page_num == page_num]

    def get_unique_fonts(self) -> dict:
        counts = {}
        for s in self._spans:
            counts[s.font_name] = counts.get(s.font_name, 0) + 1
        return counts

    # ------------------------------------------------------------------ #
    #  Font resolution (shared)                                            #
    # ------------------------------------------------------------------ #

    def _resolve_font(self, font_name: str, font_path: Optional[str] = None):
        """Return (insert_kwargs, font_used, font_preserved)."""
        if font_path:
            return {"fontfile": font_path}, font_path, True
        clean = re.sub(r'^[A-Z]{6}\+', '', font_name)
        key = clean.lower().replace(" ", "-")
        if key in BUILTIN_MAP:
            return {"fontname": BUILTIN_MAP[key]}, clean, True
        return {"fontname": "helv"}, "Helvetica (fallback)", False

    @staticmethod
    def _unpack_color(packed: int):
        r = ((packed >> 16) & 0xFF) / 255
        g = ((packed >> 8)  & 0xFF) / 255
        b = (packed & 0xFF) / 255
        return r, g, b

    # ------------------------------------------------------------------ #
    #  Text: replace in place                                             #
    # ------------------------------------------------------------------ #

    def replace_text(self, span: TextSpan, new_text: str,
                     font_path: Optional[str] = None) -> EditResult:
        page = self.doc[span.page_num]
        rect = fitz.Rect(span.bbox)
        bg = self._sample_bg(page, rect)
        page.add_redact_annot(rect, fill=bg)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        insert_kwargs, font_used, font_preserved = self._resolve_font(span.font_name, font_path)
        r, g, b = self._unpack_color(span.color)
        baseline_y = rect.y0 + span.font_size * 0.85

        try:
            page.insert_text((rect.x0, baseline_y), new_text,
                             fontsize=span.font_size, color=(r, g, b),
                             **insert_kwargs)
        except Exception as e:
            return EditResult(False, False, font_used, str(e))

        self._scan()
        return EditResult(True, font_preserved, font_used)

    # ------------------------------------------------------------------ #
    #  Text: move to new position                                          #
    # ------------------------------------------------------------------ #

    def move_text(self, span: TextSpan, new_x: float, new_y: float,
                  font_path: Optional[str] = None) -> EditResult:
        """Erase span from original position, redraw at (new_x, new_y)."""
        page = self.doc[span.page_num]
        old_rect = fitz.Rect(span.bbox)
        bg = self._sample_bg(page, old_rect)

        # Erase original
        page.add_redact_annot(old_rect, fill=bg)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        # Insert at new position
        insert_kwargs, font_used, font_preserved = self._resolve_font(span.font_name, font_path)
        r, g, b = self._unpack_color(span.color)
        h = old_rect.height
        baseline_y = new_y + h * 0.85

        try:
            page.insert_text((new_x, baseline_y), span.text,
                             fontsize=span.font_size, color=(r, g, b),
                             **insert_kwargs)
        except Exception as e:
            return EditResult(False, False, font_used, str(e))

        self._scan()
        return EditResult(True, font_preserved, font_used)

    # ------------------------------------------------------------------ #
    #  Image: replace / resize                                             #
    # ------------------------------------------------------------------ #

    def replace_image(self, page_num: int, old_bbox: list,
                      new_image_bytes: bytes, new_bbox: Optional[list] = None) -> bool:
        """Erase image at old_bbox, insert new_image_bytes at new_bbox (or old_bbox)."""
        page = self.doc[page_num]
        old_rect = fitz.Rect(old_bbox)
        target_rect = fitz.Rect(new_bbox) if new_bbox else old_rect

        # White-out old image
        page.draw_rect(old_rect, color=(1,1,1), fill=(1,1,1))

        try:
            page.insert_image(target_rect, stream=new_image_bytes)
        except Exception as e:
            print(f"[replace_image] {e}")
            return False

        self._scan()
        return True

    def resize_image(self, page_num: int, old_bbox: list, new_bbox: list) -> bool:
        """Move/resize an image by re-embedding it at a new rect."""
        page = self.doc[page_num]
        old_rect = fitz.Rect(old_bbox)

        # Extract the image bytes from the page
        img_bytes = None
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") == 1:
                bx = tuple(block["bbox"])
                if (abs(bx[0] - old_bbox[0]) < 2 and abs(bx[1] - old_bbox[1]) < 2):
                    xref = block.get("xref", 0)
                    if xref:
                        try:
                            img_bytes = self.doc.extract_image(xref)["image"]
                        except Exception:
                            pass
                    break

        if not img_bytes:
            return False

        return self.replace_image(page_num, old_bbox, img_bytes, new_bbox)

    def delete_image(self, page_num: int, bbox: list) -> bool:
        """White-out an image."""
        page = self.doc[page_num]
        rect = fitz.Rect(bbox)
        page.draw_rect(rect, color=(1,1,1), fill=(1,1,1))
        self._scan()
        return True

    # ------------------------------------------------------------------ #
    #  Background sampling                                                  #
    # ------------------------------------------------------------------ #

    def _sample_bg(self, page, rect: fitz.Rect):
        try:
            clip = rect + fitz.Rect(-2, -2, 2, 2)
            pix = page.get_pixmap(clip=clip, matrix=fitz.Matrix(1, 1))
            s = pix.samples
            if len(s) >= 3:
                return (s[0]/255, s[1]/255, s[2]/255)
        except Exception:
            pass
        return (1, 1, 1)

    # ------------------------------------------------------------------ #
    #  Render / Save                                                        #
    # ------------------------------------------------------------------ #

    def render_page(self, page_num: int, zoom: float = 1.5):
        page = self.doc[page_num]
        mat = fitz.Matrix(zoom, zoom)
        return page.get_pixmap(matrix=mat, alpha=False)

    def save(self, output_path: str):
        self.doc.save(output_path, garbage=4, deflate=True, clean=True)

    @property
    def page_count(self) -> int:
        return len(self.doc)

    @property
    def title(self) -> str:
        return self.doc.metadata.get("title", "") or self.path
