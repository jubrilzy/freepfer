"""
Font Matcher
Scans system fonts, indexes them by PostScript/full/family name,
and returns the best match for a PDF font name.
"""

import os
import sys
import re
from pathlib import Path
from typing import Optional, Dict, List, Tuple


# Known metric-equivalent substitutions (PDF name → open-source equivalent)
METRIC_SUBSTITUTES = {
    "arial":               "Liberation Sans",
    "arialbold":           "Liberation Sans Bold",
    "arialbolditalic":     "Liberation Sans Bold Italic",
    "arialitalic":         "Liberation Sans Italic",
    "timesnewroman":       "Liberation Serif",
    "timesnewromanbold":   "Liberation Serif Bold",
    "timesnewromanitalic": "Liberation Serif Italic",
    "couriernew":          "Liberation Mono",
    "calibri":             "Carlito",
    "calibribold":         "Carlito Bold",
}

FONT_EXTENSIONS = {".ttf", ".otf", ".ttc"}


def _system_font_dirs() -> List[str]:
    platform = sys.platform
    dirs = []
    if platform.startswith("linux"):
        dirs = [
            "/usr/share/fonts",
            "/usr/local/share/fonts",
            os.path.expanduser("~/.fonts"),
            os.path.expanduser("~/.local/share/fonts"),
        ]
    elif platform == "darwin":
        dirs = [
            "/Library/Fonts",
            "/System/Library/Fonts",
            "/System/Library/AssetsV2/com_apple_MobileAsset_Font7",
            os.path.expanduser("~/Library/Fonts"),
        ]
    elif platform.startswith("win"):
        dirs = [
            r"C:\Windows\Fonts",
            os.path.expanduser(r"~\AppData\Local\Microsoft\Windows\Fonts"),
        ]
    return [d for d in dirs if d and os.path.isdir(d)]


def _normalize(name: str) -> str:
    """Normalize a font name for fuzzy matching."""
    # Strip subset prefix e.g. ABCDEF+FontName
    name = re.sub(r'^[A-Z]{6}\+', '', name)
    # Lowercase, strip punctuation/spaces
    name = re.sub(r'[\s\-_,.]', '', name).lower()
    # Remove common weight/style suffixes for base matching
    return name


def _walk_fonts(dirs: List[str]):
    for d in dirs:
        for root, _, files in os.walk(d):
            for f in files:
                if Path(f).suffix.lower() in FONT_EXTENSIONS:
                    yield os.path.join(root, f)


class FontMatcher:
    def __init__(self):
        # key: normalized name → (display_name, path)
        self._index: Dict[str, Tuple[str, str]] = {}
        self._scan()

    # ------------------------------------------------------------------ #
    #  Scanning                                                            #
    # ------------------------------------------------------------------ #

    def _scan(self):
        for path in _walk_fonts(_system_font_dirs()):
            self._index_file(path)

    def _index_file(self, path: str):
        names = self._extract_names(path)
        for display_name in names:
            key = _normalize(display_name)
            if key and key not in self._index:
                self._index[key] = (display_name, path)

    def _extract_names(self, path: str) -> List[str]:
        """Extract font name strings from a font file."""
        names = []
        try:
            from fontTools.ttLib import TTFont
            tt = TTFont(path, fontNumber=0, lazy=True)
            name_table = tt.get("name")
            if name_table:
                for record in name_table.names:
                    if record.nameID in (1, 4, 6):  # family, full, postscript
                        try:
                            names.append(record.toUnicode())
                        except Exception:
                            pass
            tt.close()
        except Exception:
            # Fallback: filename stem
            names.append(Path(path).stem)
        return names

    # ------------------------------------------------------------------ #
    #  Matching                                                            #
    # ------------------------------------------------------------------ #

    def match(self, pdf_font_name: str) -> Optional[str]:
        """
        Return the filesystem path of the best matching system font,
        or None if no match.
        Priority: exact → prefix → metric substitute → partial.
        """
        norm = _normalize(pdf_font_name)

        # 1. Exact
        if norm in self._index:
            return self._index[norm][1]

        # 2. Metric substitute
        for sub_norm, sub_display in METRIC_SUBSTITUTES.items():
            if norm == sub_norm:
                sub_key = _normalize(sub_display)
                if sub_key in self._index:
                    return self._index[sub_key][1]

        # 3. Prefix match (longest match wins)
        candidates = [(k, v) for k, v in self._index.items()
                      if norm.startswith(k) or k.startswith(norm)]
        if candidates:
            candidates.sort(key=lambda x: len(x[0]), reverse=True)
            return candidates[0][1][1]

        # 4. Substring
        for key, (display, path) in self._index.items():
            if norm in key:
                return path

        return None

    def match_with_info(self, pdf_font_name: str) -> dict:
        """Return match details for UI display."""
        path = self.match(pdf_font_name)
        if path:
            norm = _normalize(pdf_font_name)
            exact = norm in self._index
            return {
                "matched": True,
                "path": path,
                "filename": os.path.basename(path),
                "exact": exact,
                "tier": "exact" if exact else "approximate",
            }
        return {
            "matched": False,
            "path": None,
            "filename": None,
            "exact": False,
            "tier": "fallback",
        }

    # ------------------------------------------------------------------ #
    #  Utilities                                                           #
    # ------------------------------------------------------------------ #

    def font_count(self) -> int:
        return len(self._index)

    def all_fonts(self) -> List[Tuple[str, str]]:
        """Return sorted list of (display_name, path)."""
        return sorted(self._index.values(), key=lambda x: x[0].lower())
