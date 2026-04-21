"""
PDF generator – rebuilds a faithful Hebrew RTL attendance report from a
varied ParsedTable using ReportLab.

Hebrew / RTL rendering
-----------------------
ReportLab does not natively support Right-to-Left text or Hebrew glyph shaping.
Two small libraries bridge the gap:
  1. arabic_reshaper  – joins Hebrew/Arabic glyphs correctly
  2. bidi.algorithm   – applies the Unicode Bidirectional Algorithm so RTL strings
                        display right-to-left when rendered left-to-right by ReportLab.

All Hebrew strings must pass through `rtl()` before being handed to ReportLab.

Font
----
FrankRuhlLibre (OFL licence, bundled in assets/) is a common Hebrew serif font.
`_register_fonts()` is called once and is idempotent.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .models import ParsedTable, ReportType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_ASSETS_DIR = Path(__file__).parent / "assets"
_FONT_REGULAR = _ASSETS_DIR / "FrankRuhlLibre-Regular.ttf"
_FONT_BOLD = _ASSETS_DIR / "FrankRuhlLibre-Bold.ttf"
_FONT_NAME = "FrankRuhlLibre"
_FONT_NAME_BOLD = "FrankRuhlLibre-Bold"
_FONTS_REGISTERED = False

# ---------------------------------------------------------------------------
# Style constants
# ---------------------------------------------------------------------------
_HEADER_FONT_SIZE = 13
_BODY_FONT_SIZE = 10
_ROW_HEIGHT = 7 * mm
_DEFAULT_COL_WIDTH = 25 * mm
_TOTAL_ROW_BG = colors.HexColor("#D9E1F2")
_ALT_ROW_BG = colors.HexColor("#F2F2F2")
_BORDER_COLOR = colors.HexColor("#4472C4")


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def rtl(text: str) -> str:
    """
    Prepare a Hebrew (or mixed Hebrew/digit) string for ReportLab rendering.

    Steps:
    1. arabic_reshaper.reshape()  – correct glyph joining
    2. bidi.algorithm.get_display() – reverse to visual RTL order
    """
    if not text:
        return text
    reshaped = arabic_reshaper.reshape(text)
    return get_display(reshaped)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _register_fonts() -> None:
    """Register Hebrew TrueType fonts with ReportLab (idempotent)."""
    global _FONTS_REGISTERED, _FONT_NAME, _FONT_NAME_BOLD
    if _FONTS_REGISTERED:
        return
    if _FONT_REGULAR.exists():
        pdfmetrics.registerFont(TTFont(_FONT_NAME, str(_FONT_REGULAR)))
    else:
        logger.warning("Hebrew font not found at %s; falling back to Helvetica.", _FONT_REGULAR)
        _FONT_NAME = "Helvetica"

    if _FONT_BOLD.exists():
        pdfmetrics.registerFont(TTFont(_FONT_NAME_BOLD, str(_FONT_BOLD)))
    else:
        _FONT_NAME_BOLD = "Helvetica-Bold"

    _FONTS_REGISTERED = True


def _make_styles() -> dict[str, ParagraphStyle]:
    """Build ParagraphStyles for header block and table cells."""
    _register_fonts()
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title",
            fontName=_FONT_NAME_BOLD,
            fontSize=_HEADER_FONT_SIZE + 2,
            leading=18,
            alignment=1,  # centre
        ),
        "subtitle": ParagraphStyle(
            "subtitle",
            fontName=_FONT_NAME,
            fontSize=_HEADER_FONT_SIZE,
            leading=16,
            alignment=1,
        ),
        "cell": ParagraphStyle(
            "cell",
            fontName=_FONT_NAME,
            fontSize=_BODY_FONT_SIZE,
            leading=12,
            alignment=2,  # right (RTL)
        ),
        "cell_bold": ParagraphStyle(
            "cell_bold",
            fontName=_FONT_NAME_BOLD,
            fontSize=_BODY_FONT_SIZE,
            leading=12,
            alignment=2,
        ),
    }


# ---------------------------------------------------------------------------
# ReportBuilder
# ---------------------------------------------------------------------------

class ReportBuilder:
    """
    Generates a PDF from a varied ParsedTable that visually mirrors the original.

    Usage
    -----
    builder = ReportBuilder()
    builder.build(table, "output.pdf", ReportType.TYPE_1)
    """

    def build(
        self,
        table: ParsedTable,
        output_path: str,
        report_type: ReportType,
    ) -> None:
        """
        Write a PDF to *output_path*.

        Parameters
        ----------
        table       : varied ParsedTable.
        output_path : destination file path (will be created/overwritten).
        report_type : used to pick layout tweaks specific to each type.
        """
        _register_fonts()
        styles = _make_styles()

        # Page size from metadata if available, otherwise A4
        page_w = table.metadata.get("page_width_pt", A4[0])
        page_h = table.metadata.get("page_height_pt", A4[1])

        doc = SimpleDocTemplate(
            output_path,
            pagesize=(page_w, page_h),
            rightMargin=15 * mm,
            leftMargin=15 * mm,
            topMargin=15 * mm,
            bottomMargin=15 * mm,
        )

        story = []

        # --- Header block ---
        story += self._build_header_block(table, styles)
        story.append(Spacer(1, 6 * mm))

        # --- Data table ---
        story.append(self._build_data_table(table, styles, report_type))

        # --- Footer / totals block ---
        story += self._build_footer_block(table, styles)

        doc.build(story)
        logger.info("PDF written to %s", output_path)

    # ------------------------------------------------------------------
    # Header block
    # ------------------------------------------------------------------

    @staticmethod
    def _build_header_block(table: ParsedTable, styles: dict) -> list:
        """Reconstruct the title / employee info block above the table."""
        meta = table.metadata
        elements = []

        title = meta.get("report_title", "דוח נוכחות")
        elements.append(Paragraph(rtl(title), styles["title"]))

        for key in ("employee_name", "department", "month_label"):
            value = meta.get(key, "")
            if value:
                elements.append(Paragraph(rtl(str(value)), styles["subtitle"]))

        return elements

    # ------------------------------------------------------------------
    # Data table
    # ------------------------------------------------------------------

    def _build_data_table(
        self,
        table: ParsedTable,
        styles: dict,
        report_type: ReportType,
    ) -> Table:
        """Build the main attendance grid."""
        col_widths = self._compute_col_widths(table)

        # Header row (columns reversed for RTL display)
        header_cells = [
            Paragraph(rtl(h), styles["cell_bold"]) for h in reversed(table.headers)
        ]
        data_grid = [header_cells]

        for row in table.rows:
            cells = [Paragraph(rtl(c), styles["cell"]) for c in reversed(row.raw_row)]
            # Rebuild raw_row from TimeEntry fields so varied values are used
            cells = self._row_to_cells(row, table, styles)
            data_grid.append(cells)

        t = Table(data_grid, colWidths=list(reversed(col_widths)), rowHeights=_ROW_HEIGHT)
        t.setStyle(self._table_style(len(data_grid)))
        return t

    @staticmethod
    def _row_to_cells(row: "TimeEntry", table: ParsedTable, styles: dict) -> list:
        """
        Build a list of Paragraph cells for one TimeEntry, in RTL display order
        (i.e. reversed column order so the rightmost column appears first).
        """
        from .models import TimeEntry  # local import to avoid circularity

        col_map = table.col_map
        n_cols = len(table.headers)
        cells = list(row.raw_row) + [""] * n_cols   # pad to at least n_cols

        # Overwrite the varied fields into the correct column positions
        _override = {
            "entry": row.entry,
            "exit": row.exit,
            "daily_total": row.daily_total,
            "employee_name": row.employee_name,
            "date": row.date,
        }
        for key, value in _override.items():
            idx = col_map.get(key)
            if idx is not None and idx < len(cells):
                cells[idx] = value

        cells = cells[:n_cols]
        return [Paragraph(rtl(c), styles["cell"]) for c in reversed(cells)]

    @staticmethod
    def _compute_col_widths(table: ParsedTable) -> list[float]:
        """Return column widths in points, from metadata or a default."""
        raw = table.metadata.get("col_widths", [])
        n = len(table.headers)
        if raw and len(raw) == n:
            # Scale raw pixel widths proportionally to fit page
            total_raw = sum(raw)
            usable = A4[0] - 30 * mm
            return [w / total_raw * usable for w in raw]
        return [_DEFAULT_COL_WIDTH] * n

    @staticmethod
    def _table_style(n_rows: int) -> TableStyle:
        """Generate a TableStyle with alternating rows, borders, bold header."""
        cmds = [
            # Outer grid
            ("GRID",        (0, 0), (-1, -1), 0.5, _BORDER_COLOR),
            # Header row
            ("BACKGROUND",  (0, 0), (-1,  0), _BORDER_COLOR),
            ("TEXTCOLOR",   (0, 0), (-1,  0), colors.white),
            ("FONTNAME",    (0, 0), (-1,  0), _FONT_NAME_BOLD),
            ("FONTSIZE",    (0, 0), (-1,  0), _BODY_FONT_SIZE),
            # Alignment
            ("ALIGN",       (0, 0), (-1, -1), "RIGHT"),
            ("VALIGN",      (0, 0), (-1, -1), "MIDDLE"),
        ]
        # Alternating row shading for data rows
        for i in range(2, n_rows, 2):
            cmds.append(("BACKGROUND", (0, i), (-1, i), _ALT_ROW_BG))
        # Last row is the total – highlight it
        if n_rows > 1:
            cmds.append(("BACKGROUND", (0, n_rows - 1), (-1, n_rows - 1), _TOTAL_ROW_BG))
            cmds.append(("FONTNAME",   (0, n_rows - 1), (-1, n_rows - 1), _FONT_NAME_BOLD))
        return TableStyle(cmds)

    # ------------------------------------------------------------------
    # Footer block
    # ------------------------------------------------------------------

    @staticmethod
    def _build_footer_block(table: ParsedTable, styles: dict) -> list:
        """Render the grand total and any signature lines from metadata."""
        elements = [Spacer(1, 4 * mm)]
        grand_total = table.metadata.get("grand_total", "")
        if grand_total:
            label = rtl(f'סה"כ שעות: {grand_total}')
            elements.append(Paragraph(label, styles["subtitle"]))
        footer_text = table.metadata.get("footer_text", "")
        if footer_text:
            elements.append(Spacer(1, 4 * mm))
            elements.append(Paragraph(rtl(footer_text), styles["cell"]))
        return elements
