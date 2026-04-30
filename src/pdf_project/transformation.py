from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import datetime

from .models import ParsedTable, ReportType, TimeEntry

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
            if norm and minutes is not None and 5 * 60 <= minutes <= 20 * 60:
                times.append(norm)
    if len(times) < 2:
        return "", ""

    unique_times = sorted({_time_to_minutes(t): t for t in times}.items())
    if len(unique_times) < 2:
        return "", ""
    return unique_times[0][1], unique_times[-1][1]


def _extract_daily_total(raw_cells: list[str], entry: str, exit_: str) -> str:
    decimal_artifact_re = re.compile(r"^\d{1,2}\.\d{2}$")

    start = _time_to_minutes(entry)
    end = _time_to_minutes(exit_)
    if start is not None and end is not None and end >= start:
        delta = end - start
        return f"{delta // 60:02d}:{delta % 60:02d}"

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
    return max(candidates, key=len) if candidates else ""


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
        0: "שני",
        1: "שלישי",
        2: "רביעי",
        3: "חמישי",
        4: "שישי",
        5: "שבת",
        6: "ראשון",
    }
    return hebrew_weekdays.get(dt.weekday(), "")


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

    return max(hebrew_candidates, key=len) if hebrew_candidates else ""


class BaseTransformationStrategy(ABC):
    @abstractmethod
    def canonical_layout(self) -> list[tuple[str, str]]:
        pass

    @abstractmethod
    def transform_row(self, row: TimeEntry) -> dict[str, str]:
        pass


class Type1TransformationStrategy(BaseTransformationStrategy):
    def canonical_layout(self) -> list[tuple[str, str]]:
        return _CANONICAL_LAYOUTS[ReportType.TYPE_1]

    def transform_row(self, row: TimeEntry) -> dict[str, str]:
        raw_cells = [c.strip() for c in row.raw_row if c and c.strip()]
        if not raw_cells:
            return {
                "date": row.date or "",
                "entry": row.entry or "",
                "exit": row.exit or "",
                "daily_total": row.daily_total or "",
            }

        date_value = row.date or _extract_date(raw_cells)
        entry_value, exit_value = row.entry, row.exit
        if not entry_value or not exit_value:
            rec_entry, rec_exit = _extract_entry_exit(raw_cells)
            entry_value = entry_value or rec_entry
            exit_value = exit_value or rec_exit

        daily_total = row.daily_total or _extract_daily_total(raw_cells, entry_value, exit_value)
        return {
            "date": date_value or "",
            "entry": entry_value or "",
            "exit": exit_value or "",
            "daily_total": daily_total or "",
        }


class Type2TransformationStrategy(Type1TransformationStrategy):
    def canonical_layout(self) -> list[tuple[str, str]]:
        return _CANONICAL_LAYOUTS[ReportType.TYPE_2]

    def transform_row(self, row: TimeEntry) -> dict[str, str]:
        base_values = super().transform_row(row)
        raw_cells = [c.strip() for c in row.raw_row if c and c.strip()]

        weekday_value = _extract_weekday(raw_cells)
        if not weekday_value:
            weekday_value = _weekday_from_date_string(base_values.get("date", ""))

        base_values["weekday"] = weekday_value
        base_values["notes"] = _extract_notes(raw_cells)
        return base_values


class TransformationService:
    def __init__(self, registry: dict[ReportType, BaseTransformationStrategy] | None = None):
        if registry is None:
            self._registry = {
                ReportType.TYPE_1: Type1TransformationStrategy(),
                ReportType.TYPE_2: Type2TransformationStrategy(),
            }
        else:
            self._registry = registry

    def strategy_for(self, report_type: ReportType) -> BaseTransformationStrategy:
        strategy = self._registry.get(report_type)
        if strategy is None:
            raise ValueError(f"No transformation strategy registered for report type: {report_type}")
        return strategy

    def normalise_table_headers(self, table: ParsedTable, report_type: ReportType) -> ParsedTable:
        strategy = self.strategy_for(report_type)
        layout = strategy.canonical_layout()
        canonical_headers = [header for _, header in layout]
        canonical_col_map = {key: idx for idx, (key, _) in enumerate(layout)}
        source_col_map = dict(table.col_map)

        normalized_rows: list[TimeEntry] = []
        for row in table.rows:
            reconstructed = strategy.transform_row(row)

            def _canonical_value(key: str) -> str:
                current = reconstructed.get(key, "")
                if current:
                    return current
                src_idx = source_col_map.get(key)
                if src_idx is not None and src_idx < len(row.raw_row):
                    raw_value = row.raw_row[src_idx]
                    return (raw_value or "").strip()
                return ""

            canonical_raw = [_canonical_value(key) for key, _ in layout]
            has_times = bool(reconstructed.get("entry") and reconstructed.get("exit"))

            normalized_rows.append(
                replace(
                    row,
                    date=reconstructed.get("date") or row.date or "",
                    entry=reconstructed.get("entry") or row.entry or "",
                    exit=reconstructed.get("exit") or row.exit or "",
                    daily_total=reconstructed.get("daily_total") or row.daily_total or "",
                    is_special=row.is_special and not has_times,
                    raw_row=canonical_raw,
                )
            )

        return ParsedTable(
            headers=canonical_headers,
            rows=normalized_rows,
            col_map=canonical_col_map,
            metadata={**table.metadata, "canonical_headers": True},
        )
