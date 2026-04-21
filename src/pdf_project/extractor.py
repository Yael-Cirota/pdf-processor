"""
Table extractor – maps OCR word tokens into a structured ParsedTable.

Strategy
--------
1. img2table detects cell bounding boxes from the page image.
2. OCR words are assigned to cells by spatial overlap.
3. The first non-empty row is treated as the header row.
4. Headers are normalised from Hebrew to internal English keys via a
   vocabulary lookup, so the rest of the pipeline is language-agnostic.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PIL import Image

from .models import ParsedTable, TimeEntry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hebrew → internal-key vocabulary
# ---------------------------------------------------------------------------
# Keys are lower-cased, stripped substrings that appear in typical Hebrew
# attendance report column headers.
_HEB_TO_KEY: dict[str, str] = {
    # Date column variants
    "תאריך": "date",
    "יום": "date",
    # Entry / clock-in
    "כניסה": "entry",
    "כניסה לעבודה": "entry",
    "שעת כניסה": "entry",
    # Exit / clock-out
    "יציאה": "exit",
    "יציאה מעבודה": "exit",
    "שעת יציאה": "exit",
    # Daily total hours
    'סה"כ': "daily_total",
    "סהכ": "daily_total",
    "שעות": "daily_total",
    "שעות יומיות": "daily_total",
    "סה\"כ יומי": "daily_total",
    # Employee name (Type-2 reports)
    "שם עובד": "employee_name",
    "עובד": "employee_name",
    "שם": "employee_name",
    # Monthly total (Type-2 footer rows)
    'סה"כ חודשי': "monthly_total",
    "סהכ חודשי": "monthly_total",
    "חודשי": "monthly_total",
}

# Phrases that indicate a non-work row (holiday, absence, etc.)
_SPECIAL_KEYWORDS = {
    "חופשה", "מחלה", "שבת", "חג", "היעדרות", "שבתון", "אחר",
    "holiday", "sick", "absent", "vacation",
}


class TableExtractor:
    """
    Extracts a ParsedTable from a list of page images and their OCR data.

    Usage
    -----
    extractor = TableExtractor()
    table = extractor.extract(images, ocr_pages)
    """

    def extract(
        self,
        images: list[Image.Image],
        ocr_pages: list[list[dict]],
    ) -> ParsedTable:
        """
        Combine img2table cell detection with OCR word tokens into a ParsedTable.

        Parameters
        ----------
        images    : PIL images, one per PDF page (from PDFScanner.pdf_to_images).
        ocr_pages : word-level OCR dicts per page (from PDFScanner.ocr_pdf).
        """
        all_raw_rows: list[list[str]] = []
        metadata: dict[str, Any] = {
            "page_count": len(images),
            "col_widths": [],
            "row_heights": [],
        }

        for page_idx, (img, words) in enumerate(zip(images, ocr_pages)):
            raw_rows, col_widths, row_heights = self._extract_page(img, words, page_idx)
            all_raw_rows.extend(raw_rows)
            if not metadata["col_widths"] and col_widths:
                metadata["col_widths"] = col_widths
                metadata["row_heights"] = row_heights

        if not all_raw_rows:
            logger.warning("No table rows detected in the document.")
            return ParsedTable(metadata=metadata)

        # First non-empty row is the header
        headers = all_raw_rows[0]
        data_rows = all_raw_rows[1:]

        col_map = self._build_col_map(headers)
        rows = [self._parse_row(raw, col_map) for raw in data_rows]

        return ParsedTable(
            headers=headers,
            rows=rows,
            col_map=col_map,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_page(
        image: Image.Image,
        words: list[dict],
        page_idx: int,
    ) -> tuple[list[list[str]], list[int], list[int]]:
        """
        Use img2table to detect the table grid, then fill cells with OCR text.

        Returns (raw_rows, col_widths, row_heights).
        """
        import tempfile, os
        from img2table.document import Image as Img2Img  # deferred: pulls pandas

        # img2table needs a file path or bytes; write to a temp file
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            image.save(tmp.name)
            tmp_path = tmp.name

        try:
            doc = Img2Img(src=tmp_path)
            tables = doc.extract_tables(
                implicit_rows=True,
                borderless_tables=True,
            )
        finally:
            os.unlink(tmp_path)

        if not tables:
            logger.warning("No table detected on page %d.", page_idx + 1)
            return [], [], []

        # Use the largest detected table
        table = max(tables, key=lambda t: len(t.content))

        raw_rows: list[list[str]] = []
        col_widths: list[int] = []
        row_heights: list[int] = []

        for row_idx, row in enumerate(table.content.values()):
            row_cells: list[str] = []
            for col_idx, cell in enumerate(row):
                # Map OCR words into this cell by bounding-box overlap
                cell_text = _words_in_bbox(
                    words,
                    bbox=(cell.bbox.x1, cell.bbox.y1, cell.bbox.x2, cell.bbox.y2),
                )
                row_cells.append(cell_text)
                if row_idx == 0:
                    col_widths.append(cell.bbox.x2 - cell.bbox.x1)
            if row_idx < len(table.content):
                first_cell = list(row)[0]
                row_heights.append(first_cell.bbox.y2 - first_cell.bbox.y1)
            raw_rows.append(row_cells)

        return raw_rows, col_widths, row_heights

    @staticmethod
    def _build_col_map(headers: list[str]) -> dict[str, int]:
        """Return {internal_key: column_index} for recognised header strings."""
        col_map: dict[str, int] = {}
        for idx, h in enumerate(headers):
            normalised = h.strip()
            for heb, key in _HEB_TO_KEY.items():
                if heb in normalised:
                    if key not in col_map:   # First match wins
                        col_map[key] = idx
                    break
        return col_map

    @staticmethod
    def _parse_row(raw: list[str], col_map: dict[str, int]) -> TimeEntry:
        """Convert a list of raw cell strings into a TimeEntry."""

        def _get(key: str) -> str:
            idx = col_map.get(key)
            return raw[idx].strip() if idx is not None and idx < len(raw) else ""

        date = _get("date")
        entry = _get("entry")
        exit_ = _get("exit")
        daily_total = _get("daily_total")
        employee_name = _get("employee_name")

        # Detect special rows (absence, holiday, etc.)
        row_text = " ".join(raw).strip()
        is_special = (
            not entry and not exit_
        ) or any(kw in row_text for kw in _SPECIAL_KEYWORDS)

        return TimeEntry(
            date=date,
            entry=entry,
            exit=exit_,
            daily_total=daily_total,
            employee_name=employee_name,
            is_special=is_special,
            raw_row=raw,
        )


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _words_in_bbox(words: list[dict], bbox: tuple[int, int, int, int]) -> str:
    """
    Collect all OCR words whose centre falls within *bbox* = (x1, y1, x2, y2).
    Returns the words joined by a single space (RTL order is handled by the
    bidi algorithm in the generator, not here).
    """
    x1, y1, x2, y2 = bbox
    matched: list[tuple[int, str]] = []   # (left_x, text) for sort
    for w in words:
        cx = w["left"] + w["width"] // 2
        cy = w["top"] + w["height"] // 2
        if x1 <= cx <= x2 and y1 <= cy <= y2:
            matched.append((w["left"], w["text"]))
    # Sort left-to-right; RTL rendering is the generator's job
    matched.sort(key=lambda t: t[0])
    return " ".join(t for _, t in matched)
