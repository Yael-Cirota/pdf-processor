"""
Main entry point – orchestrates the full pipeline and exposes a CLI.

Pipeline
--------
1. PDFScanner    → rasterise PDF pages + OCR
2. TableExtractor → detect table grid + build ParsedTable
3. classify()    → determine ReportType (TYPE_1 or TYPE_2)
4. Variator      → apply deterministic seed-based logical changes
5. ReportBuilder → write a new PDF that mirrors the original layout

CLI usage
---------
    pdf-vary --input report.pdf --output varied.pdf [--seed 42] [--type auto|1|2]

Programmatic usage
------------------
    from pdf_project.main import process
    process("input.pdf", "output.pdf", seed=42)
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from .classifier import classify
from .extractor import TableExtractor
from .generator import ReportBuilder
from .models import ParsedTable, ReportType, TimeEntry
from .ocr import PDFScanner
from .parser import get_parser
from .transformation import TransformationService
from .variation import BaseVariator, Type1Variator, Type2Variator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Variator registry – extend here to support new report types
# ---------------------------------------------------------------------------
VARIATOR_MAP: dict[ReportType, type[BaseVariator]] = {
    ReportType.TYPE_1: Type1Variator,
    ReportType.TYPE_2: Type2Variator,
}

_CANONICAL_LAYOUTS: dict[ReportType, list[tuple[str, str]]] = {
    ReportType.TYPE_1: [
        ("date", "תאריך"),
        ("entry", "כניסה"),
        ("exit", "יציאה"),
        ("daily_total", 'סה"כ'),
    ],
    ReportType.TYPE_2: [
        ("date", "תאריך"),
        ("weekday", "יום בשבוע"),
        ("entry", "שעת כניסה"),
        ("exit", "שעת יציאה"),
        ("daily_total", 'סה"כ שעות'),
        ("notes", "הערות"),
    ],
}

_TRANSFORMATION_SERVICE = TransformationService()


def _type_label_for_filename(report_type: ReportType) -> str:
    """Map internal report type enum to the requested human-readable label."""
    return "type A" if report_type == ReportType.TYPE_1 else "type B"


def _resolve_final_output_path(
    input_path: Path,
    output_path: Path,
    report_type: ReportType,
) -> Path:
    """
    Build the final output path using:
    - output directory from *output_path*
    - filename pattern: "type A|B - <input_stem>.pdf"

    If *output_path* points to a directory (existing or suffix-less), that
    directory is used directly; otherwise its parent directory is used.
    """
    if output_path.exists() and output_path.is_dir():
        out_dir = output_path
    elif output_path.suffix:
        out_dir = output_path.parent
    else:
        out_dir = output_path

    final_name = f"{_type_label_for_filename(report_type)} - {input_path.stem}.pdf"
    return out_dir / final_name


def _row_value_by_key(row: TimeEntry, key: str) -> str:
    """Return the row field value for an internal column key."""
    values = {
        "date": row.date,
        "entry": row.entry,
        "exit": row.exit,
        "daily_total": row.daily_total,
    }
    return values.get(key, "")


_DATE_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b")
_TIME_RE = re.compile(r"\b(?:[01]?\d|2[0-3])[:.]?[0-5]\d\b")


def _normalise_time_token(token: str) -> str:
    token = token.strip()
    if not token:
        return ""
    if ":" in token:
        return token
    if "." in token:
        parts = token.split(".")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    if token.isdigit() and len(token) in (3, 4):
        if len(token) == 3:
            token = f"0{token}"
        return f"{token[:2]}:{token[2:]}"
    return ""


def _time_to_minutes(value: str) -> int | None:
    try:
        hh, mm = value.split(":", 1)
        h = int(hh)
        m = int(mm)
    except Exception:
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h * 60 + m
    return None


def _extract_date(raw_cells: list[str]) -> str:
    for cell in reversed(raw_cells):
        match = _DATE_RE.search(cell)
        if match:
            return match.group(0)
    return ""


def _extract_entry_exit(raw_cells: list[str]) -> tuple[str, str]:
    times: list[str] = []
    for cell in raw_cells:
        for token in _TIME_RE.findall(cell):
            norm = _normalise_time_token(token)
            minutes = _time_to_minutes(norm)
            # Working hours are expected in daytime; filters out many 00:xx totals.
            if norm and minutes is not None and 5 * 60 <= minutes <= 20 * 60:
                times.append(norm)
    if len(times) < 2:
        return "", ""

    unique_times = sorted({_time_to_minutes(t): t for t in times}.items())
    if len(unique_times) < 2:
        return "", ""
    entry = unique_times[0][1]
    exit_ = unique_times[-1][1]
    return entry, exit_


def _extract_daily_total(raw_cells: list[str], entry: str, exit_: str) -> str:
    # OCR frequently emits decimal artifacts like 0.00 / 7.50; ignore these
    # and prefer computed duration from entry/exit when available.
    decimal_artifact_re = re.compile(r"^\d{1,2}\.\d{2}$")

    start = _time_to_minutes(entry)
    end = _time_to_minutes(exit_)
    if start is not None and end is not None and end >= start:
        delta = end - start
        return f"{delta // 60:02d}:{delta % 60:02d}"

    # Fallback: use explicit HH:MM-like durations from raw cells.
    for cell in raw_cells:
        for token in _TIME_RE.findall(cell):
            if decimal_artifact_re.match(token.strip()):
                continue
            norm = _normalise_time_token(token)
            if norm and norm not in {entry, exit_}:
                minutes = _time_to_minutes(norm)
                if minutes is not None and minutes <= 16 * 60:
                    return norm
    return ""


def _extract_employee_name(raw_cells: list[str]) -> str:
    candidates: list[str] = []
    for cell in raw_cells:
        text = cell.strip().strip("|")
        if not text:
            continue
        if _DATE_RE.search(text) or _TIME_RE.search(text):
            continue
        if any(ch.isalpha() for ch in text):
            candidates.append(text)
    if not candidates:
        return ""
    return max(candidates, key=len)


def _extract_weekday(raw_cells: list[str]) -> str:
    weekday_tokens = (
        "ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת",
        "sun", "mon", "tue", "wed", "thu", "fri", "sat",
    )
    for cell in raw_cells:
        lowered = cell.lower()
        for token in weekday_tokens:
            if token in lowered:
                return cell.strip().strip("|")
    return ""


def _weekday_from_date_string(date_value: str) -> str:
    """Return Hebrew weekday name from a date string like dd/mm/yy or dd/mm/yyyy."""
    if not date_value:
        return ""

    match = re.search(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", date_value)
    if not match:
        return ""

    day = int(match.group(1))
    month = int(match.group(2))
    year_part = match.group(3)
    if not year_part:
        return ""

    year = int(year_part)
    if year < 100:
        year += 2000

    try:
        dt = datetime(year, month, day)
    except ValueError:
        return ""

    hebrew_weekdays = {
        0: "שני",     # Monday
        1: "שלישי",   # Tuesday
        2: "רביעי",   # Wednesday
        3: "חמישי",   # Thursday
        4: "שישי",    # Friday
        5: "שבת",     # Saturday
        6: "ראשון",   # Sunday
    }
    return hebrew_weekdays.get(dt.weekday(), "")


def _parse_date_string(date_value: str) -> datetime | None:
    """Parse dd/mm/yy or dd/mm/yyyy from OCR text."""
    if not date_value:
        return None
    match = re.search(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", date_value)
    if not match:
        return None
    day = int(match.group(1))
    month = int(match.group(2))
    year_part = match.group(3)
    if not year_part:
        return None
    year = int(year_part)
    if year < 100:
        year += 2000
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def _date_key_from_text(date_value: str) -> str | None:
    """Normalize date text into a stable YYYY-MM-DD key."""
    dt = _parse_date_string(date_value)
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%d")


def _build_type2_row_hints_from_ocr(ocr_pages: list[list[dict]]) -> dict[str, dict[str, str]]:
    """
    Build date-indexed Type-2 row hints (entry/exit/total) from OCR text lines.
    This uses only text that exists in the input page, without fabricating dates.
    """
    if not ocr_pages or not ocr_pages[0]:
        return {}

    # Group words by approximate line y-coordinate.
    lines: dict[int, list[dict]] = {}
    for w in ocr_pages[0]:
        y = round(w["top"] / 8) * 8
        lines.setdefault(y, []).append(w)

    hints: dict[str, dict[str, str]] = {}
    for _, words in sorted(lines.items()):
        line_text = " ".join(w["text"] for w in sorted(words, key=lambda z: z["left"]))
        date_match = _DATE_RE.search(line_text)
        if not date_match:
            continue

        date_key = _date_key_from_text(date_match.group(0))
        if not date_key:
            continue

        times: list[tuple[int, str]] = []
        for token in _TIME_RE.findall(line_text):
            norm = _normalise_time_token(token)
            minutes = _time_to_minutes(norm)
            if norm and minutes is not None and 5 * 60 <= minutes <= 20 * 60:
                times.append((minutes, norm))

        if len(times) < 2:
            continue

        # Remove duplicates by minute-value and sort ascending.
        uniq = {m: t for m, t in times}
        ordered = sorted(uniq.items(), key=lambda x: x[0])
        entry = ordered[0][1]
        exit_ = ordered[-1][1]
        if entry == exit_:
            continue

        daily_total = ""
        start = _time_to_minutes(entry)
        end = _time_to_minutes(exit_)
        if start is not None and end is not None and end >= start:
            delta = end - start
            daily_total = f"{delta // 60:02d}:{delta % 60:02d}"

        hints[date_key] = {
            "entry": entry,
            "exit": exit_,
            "daily_total": daily_total,
        }

    return hints


def _extract_type2_summary_metadata_from_ocr(ocr_pages: list[list[dict]]) -> dict[str, str]:
    """Best-effort extraction of top summary values for Type-2 header table."""
    if not ocr_pages or not ocr_pages[0]:
        return {}

    words = ocr_pages[0]
    page_height = max((w["top"] + w["height"] for w in words), default=0)
    if page_height <= 0:
        return {}

    top_words = [w for w in words if w["top"] <= int(page_height * 0.35)]
    if not top_words:
        return {}

    lines: dict[int, list[dict]] = {}
    for w in top_words:
        y = round(w["top"] / 8) * 8
        lines.setdefault(y, []).append(w)

    line_texts = [
        " ".join(w["text"] for w in sorted(ws, key=lambda z: z["left"]))
        for _, ws in sorted(lines.items())
    ]
    joined = " | ".join(line_texts)

    result: dict[str, str] = {}

    # Employee name: prefer a line with 2+ Hebrew words.
    heb_line_re = re.compile(r"[\u0590-\u05FF]{2,}(?:\s+[\u0590-\u05FF]{2,})+")
    for line in line_texts:
        m = heb_line_re.search(line)
        if m:
            candidate = m.group(0).strip()
            if "דוח" not in candidate and "כרטיס" not in candidate:
                result["employee_name"] = candidate
                break

    # Numeric hints for rate/payment (if labels are OCR-visible).
    number_re = re.compile(r"\d{1,4}(?:[\.,]\d{1,2})?")
    pay_match = re.search(r"(?:לתשלום|תשלום)[^\d]{0,10}(\d{1,5}(?:[\.,]\d{1,2})?)", joined)
    rate_match = re.search(r"(?:לשעה|לשעת|מחיר)[^\d]{0,10}(\d{1,4}(?:[\.,]\d{1,2})?)", joined)

    if rate_match:
        result["hour_rate"] = rate_match.group(1).replace(",", ".")
    if pay_match:
        result["payment_total"] = pay_match.group(1).replace(",", ".")

    # Very weak fallback: if labels were not readable, keep empty rather than guess wildly.
    if "hour_rate" not in result and "payment_total" not in result:
        nums = [m.group(0).replace(",", ".") for m in number_re.finditer(joined)]
        if nums:
            # Avoid obvious time/date fragments.
            filtered = [n for n in nums if not re.match(r"^(?:[0-2]?\d[\.:][0-5]\d|\d{1,2}/\d{1,2})$", n)]
            if filtered:
                # Keep only one likely payment-like value if it's large enough.
                large = [n for n in filtered if float(n) >= 200]
                if large:
                    result.setdefault("payment_total", large[-1])

    return result


def _apply_type2_row_hints(table: ParsedTable, hints: dict[str, dict[str, str]]) -> ParsedTable:
    """Fill missing Type-2 row times using OCR-line hints mapped by date."""
    if not hints:
        return table

    updated_rows: list[TimeEntry] = []
    for row in table.rows:
        date_key = _date_key_from_text(row.date)
        hint = hints.get(date_key) if date_key else None
        if not hint:
            updated_rows.append(row)
            continue

        entry = row.entry or hint.get("entry", "")
        exit_ = row.exit or hint.get("exit", "")
        daily_total = row.daily_total
        if not daily_total and entry and exit_:
            start = _time_to_minutes(entry)
            end = _time_to_minutes(exit_)
            if start is not None and end is not None and end >= start:
                delta = end - start
                daily_total = f"{delta // 60:02d}:{delta % 60:02d}"

        updated_rows.append(
            replace(
                row,
                entry=entry,
                exit=exit_,
                daily_total=daily_total,
                is_special=row.is_special and not (entry and exit_),
            )
        )

    return ParsedTable(
        headers=table.headers,
        rows=updated_rows,
        col_map=table.col_map,
        metadata=table.metadata,
    )


def _fill_missing_type2_dates(rows: list[TimeEntry]) -> None:
    """Fill blank Type-2 date/weekday cells from neighboring chronological rows."""
    if not rows:
        return

    parsed_dates: list[datetime | None] = [_parse_date_string(r.date) for r in rows]

    def _next_known(start: int) -> int | None:
        for idx in range(start, len(parsed_dates)):
            if parsed_dates[idx] is not None:
                return idx
        return None

    def _prev_known(start: int) -> int | None:
        for idx in range(start, -1, -1):
            if parsed_dates[idx] is not None:
                return idx
        return None

    # Pass 1: fill gaps bracketed by two known dates when spacing is consistent.
    for i, dt in enumerate(parsed_dates):
        if dt is not None:
            continue
        j = _prev_known(i - 1)
        k = _next_known(i + 1)
        if j is None or k is None:
            continue
        gap_rows = k - j
        gap_days = (parsed_dates[k] - parsed_dates[j]).days
        if gap_days == gap_rows:
            parsed_dates[i] = parsed_dates[j] + timedelta(days=i - j)

    # Pass 2: forward propagate where still blank.
    for i, dt in enumerate(parsed_dates):
        if dt is not None:
            continue
        j = _prev_known(i - 1)
        if j is None:
            continue
        candidate = parsed_dates[j] + timedelta(days=i - j)
        k = _next_known(i + 1)
        if k is None or candidate < parsed_dates[k]:
            parsed_dates[i] = candidate

    # Write back recovered date/weekday into normalized rows.
    for i, dt in enumerate(parsed_dates):
        if dt is None or rows[i].date:
            continue
        date_str = f"{dt.day}/{dt.month}/{dt.year % 100:02d}"
        weekday = _weekday_from_date_string(date_str)
        raw = list(rows[i].raw_row)
        if len(raw) >= 1:
            raw[0] = date_str
        if len(raw) >= 2 and not raw[1].strip():
            raw[1] = weekday
        rows[i] = replace(rows[i], date=date_str, raw_row=raw)


def _extract_notes(raw_cells: list[str]) -> str:
    known_keywords: dict[str, str] = {
        "חופשה": "חופשה",
        "חופש": "חופשה",
        "מחלה": "מחלה",
        "שבת": "שבת",
        "חג": "חג",
        "ערב חג": "ערב חג",
        "היעדר": "היעדרות",
        "היעדרות": "היעדרות",
        "מילואים": "מילואים",
        "איחור": "איחור",
        "הערה": "הערה",
        "מיוחד": "מיוחד",
        "vacation": "חופשה",
        "holiday": "חג",
        "sick": "מחלה",
        "absence": "היעדרות",
        "late": "איחור",
    }

    blocked_headers = {
        "תאריך",
        "יום בשבוע",
        "שעת כניסה",
        "שעת יציאה",
        'סה"כ שעות',
        "הערות",
    }

    def _is_hebrew_char(ch: str) -> bool:
        return "\u0590" <= ch <= "\u05FF"

    def _is_meaningful_hebrew(text: str) -> bool:
        if not text or text in blocked_headers:
            return False
        if _DATE_RE.search(text) or _TIME_RE.search(text):
            return False

        hebrew_count = sum(1 for ch in text if _is_hebrew_char(ch))
        latin_count = sum(1 for ch in text if ch.isalpha() and not _is_hebrew_char(ch))
        digit_count = sum(1 for ch in text if ch.isdigit())

        # Keep only phrases that are mostly Hebrew letters.
        if hebrew_count < 3:
            return False
        if hebrew_count <= (latin_count + digit_count):
            return False

        cleaned = re.sub(r"[^\u0590-\u05FF\s]", " ", text)
        words = [w for w in cleaned.split() if len(w) >= 2]
        return len(words) >= 1

    hebrew_candidates: list[str] = []

    for cell in raw_cells:
        text = cell.strip().strip("|")
        if not text:
            continue
        if _DATE_RE.search(text) or _TIME_RE.search(text):
            continue
        lowered = text.lower()
        for keyword, canonical in known_keywords.items():
            if keyword in lowered:
                return canonical

        if _is_meaningful_hebrew(text):
            hebrew_candidates.append(text)

    if hebrew_candidates:
        # Prefer the longest meaningful Hebrew phrase.
        return max(hebrew_candidates, key=len)

    # Prefer empty notes over OCR garbage when no reliable note was found.
    return ""


def _reconstruct_row_values(row: TimeEntry, report_type: ReportType) -> dict[str, str]:
    """Recover structured row values from OCR raw cells when header mapping fails."""
    raw_cells = [c.strip() for c in row.raw_row if c and c.strip()]
    if not raw_cells:
        return {
            "employee_name": row.employee_name,
            "date": row.date,
            "entry": row.entry,
            "exit": row.exit,
            "daily_total": row.daily_total,
        }

    date_value = row.date or _extract_date(raw_cells)
    entry_value, exit_value = row.entry, row.exit
    if not entry_value or not exit_value:
        rec_entry, rec_exit = _extract_entry_exit(raw_cells)
        entry_value = entry_value or rec_entry
        exit_value = exit_value or rec_exit

    daily_total = row.daily_total or _extract_daily_total(raw_cells, entry_value, exit_value)
    employee_name = row.employee_name
    weekday_value = ""
    notes_value = ""
    if report_type == ReportType.TYPE_2:
        employee_name = employee_name or _extract_employee_name(raw_cells)
        weekday_value = _extract_weekday(raw_cells)
        if not weekday_value:
            weekday_value = _weekday_from_date_string(date_value)
        notes_value = _extract_notes(raw_cells)

    return {
        "date": date_value,
        "weekday": weekday_value,
        "entry": entry_value,
        "exit": exit_value,
        "daily_total": daily_total,
        "notes": notes_value,
    }


def _normalise_table_headers(table: ParsedTable, report_type: ReportType) -> ParsedTable:
    """Backwards-compatible wrapper around the strategy-based transformation service."""
    return _TRANSFORMATION_SERVICE.normalise_table_headers(table, report_type)


# ---------------------------------------------------------------------------
# Core processing function (public API)
# ---------------------------------------------------------------------------

def process(
    input_path: str | Path,
    output_path: str | Path,
    seed: int = 42,
    report_type: str = "auto",
    tesseract_cmd: str | None = None,
    poppler_path: str | None = None,
) -> ReportType:
    """
    Run the full pipeline on *input_path* and write the varied PDF to *output_path*.

    Parameters
    ----------
    input_path    : path to the source scanned PDF.
    output_path   : destination path for the generated PDF.
    seed          : RNG seed for reproducible variation (default 42).
    report_type   : "auto" (detect), "1" or "2" to force a specific type.
    tesseract_cmd : optional path to the Tesseract binary.
    poppler_path  : optional path to the Poppler bin directory.

    Returns
    -------
    The detected (or forced) ReportType.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    logger.info("=== pdf-vary pipeline starting ===")
    logger.info("Input : %s", input_path)
    logger.info("Output: %s", output_path)
    logger.info("Seed  : %d", seed)

    # Step 1 – OCR
    scanner = PDFScanner(tesseract_cmd=tesseract_cmd, poppler_path=poppler_path)
    logger.info("Step 1: Scanning & OCR …")
    images = scanner.pdf_to_images(input_path)
    ocr_pages = [scanner.ocr_page(scanner.preprocess(img)) for img in images]

    # Step 2 – Table extraction
    logger.info("Step 2: Extracting table …")
    extractor = TableExtractor()
    table = extractor.extract(images, ocr_pages)

    if not table.rows:
        raise ValueError("No attendance rows could be extracted from the PDF.")

    # Step 3 – Classification
    if report_type == "auto":
        detected_type = classify(table)
    elif report_type == "1":
        detected_type = ReportType.TYPE_1
    elif report_type == "2":
        detected_type = ReportType.TYPE_2
    else:
        raise ValueError(f"Invalid report_type '{report_type}'. Use 'auto', '1', or '2'.")

    logger.info("Step 3: Report type → %s", detected_type.value)

    parser = get_parser(detected_type, _TRANSFORMATION_SERVICE)
    table = parser.parse(table, ocr_pages)

    # Step 4 – Variation (disabled: keep original times unchanged)
    logger.info("Step 4: Variation disabled; preserving original times.")
    varied_table = table

    # Step 5 – Generate output PDF
    logger.info("Step 5: Generating output PDF …")
    final_output_path = _resolve_final_output_path(input_path, output_path, detected_type)
    builder = ReportBuilder()
    final_output_path.parent.mkdir(parents=True, exist_ok=True)
    builder.build(varied_table, str(final_output_path), detected_type)

    logger.info("=== Done. Output: %s ===", final_output_path)
    return detected_type


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="pdf-vary",
        description="Generate a logical variation of a scanned Hebrew attendance report PDF.",
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        metavar="INPUT",
        help="Path to the source attendance PDF.",
    )
    parser.add_argument(
        "--output", "-o",
        required=True,
        metavar="OUTPUT",
        help=(
            "Destination directory (or file path whose parent will be used). "
            "Final filename is auto-generated as 'type A|B - <input_name>.pdf'."
        ),
    )
    parser.add_argument(
        "--seed", "-s",
        type=int,
        default=42,
        metavar="SEED",
        help="Integer seed for reproducible variation (default: 42).",
    )
    parser.add_argument(
        "--type", "-t",
        dest="report_type",
        default="auto",
        choices=["auto", "1", "2"],
        help="Force report type ('1' or '2') or auto-detect (default: auto).",
    )
    parser.add_argument(
        "--tesseract",
        metavar="PATH",
        default=None,
        help="Path to the Tesseract binary (optional; uses PATH if omitted).",
    )
    parser.add_argument(
        "--poppler",
        metavar="PATH",
        default=None,
        help="Path to the Poppler bin directory (optional; uses PATH if omitted).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug-level logging.",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        detected = process(
            input_path=args.input,
            output_path=args.output,
            seed=args.seed,
            report_type=args.report_type,
            tesseract_cmd=args.tesseract,
            poppler_path=args.poppler,
        )
        final_output_path = _resolve_final_output_path(
            input_path=Path(args.input),
            output_path=Path(args.output),
            report_type=detected,
        )
        print(f"Done. Report type: {detected.value}. Output: {final_output_path}")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()
