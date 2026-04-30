from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import datetime

from .models import ParsedTable, ReportType, TimeEntry
from .transformation import TransformationService, _normalise_time_token, _time_to_minutes

_DATE_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b")
_TIME_RE = re.compile(r"\b(?:[01]?\d|2[0-3])[:.]?[0-5]\d\b")


class BaseParser(ABC):
    report_type: ReportType

    def __init__(self, transformation_service: TransformationService):
        self._transformation_service = transformation_service

    def parse(self, table: ParsedTable, ocr_pages: list[list[dict]]) -> ParsedTable:
        parsed_rows: list[TimeEntry] = []
        for row in table.rows:
            if self._is_header_line(row):
                continue
            parsed_rows.append(self._parse_row(row))

        parsed_table = ParsedTable(
            headers=table.headers,
            rows=parsed_rows,
            col_map=table.col_map,
            metadata=table.metadata,
        )

        parsed_table = self._parse_summary(parsed_table, ocr_pages)
        return self._transformation_service.normalise_table_headers(parsed_table, self.report_type)

    @abstractmethod
    def _parse_summary(self, table: ParsedTable, ocr_pages: list[list[dict]]) -> ParsedTable:
        pass

    @abstractmethod
    def _parse_row(self, row: TimeEntry) -> TimeEntry:
        pass

    @abstractmethod
    def _is_header_line(self, row: TimeEntry) -> bool:
        pass


class Type1Parser(BaseParser):
    report_type = ReportType.TYPE_1

    def _parse_summary(self, table: ParsedTable, ocr_pages: list[list[dict]]) -> ParsedTable:
        return table

    def _parse_row(self, row: TimeEntry) -> TimeEntry:
        return row

    def _is_header_line(self, row: TimeEntry) -> bool:
        return False


class Type2Parser(BaseParser):
    report_type = ReportType.TYPE_2

    def _parse_summary(self, table: ParsedTable, ocr_pages: list[list[dict]]) -> ParsedTable:
        ocr_hints = _build_type2_row_hints_from_ocr(ocr_pages)
        updated = _apply_type2_row_hints(table, ocr_hints)

        summary_meta = _extract_type2_summary_metadata_from_ocr(ocr_pages)
        if not summary_meta:
            return updated

        return ParsedTable(
            headers=updated.headers,
            rows=updated.rows,
            col_map=updated.col_map,
            metadata={**updated.metadata, **summary_meta},
        )

    def _parse_row(self, row: TimeEntry) -> TimeEntry:
        return row

    def _is_header_line(self, row: TimeEntry) -> bool:
        row_text = " ".join(cell.strip() for cell in row.raw_row if cell).strip()
        header_tokens = ("תאריך", "שעת כניסה", "שעת יציאה", "סה\"כ שעות", "הערות")
        return any(token in row_text for token in header_tokens)


PARSER_REGISTRY: dict[ReportType, type[BaseParser]] = {
    ReportType.TYPE_1: Type1Parser,
    ReportType.TYPE_2: Type2Parser,
}


def get_parser(report_type: ReportType, transformation_service: TransformationService) -> BaseParser:
    parser_cls = PARSER_REGISTRY.get(report_type)
    if parser_cls is None:
        raise ValueError(f"No parser registered for report type: {report_type}")
    return parser_cls(transformation_service)


def _parse_date_string(date_value: str) -> datetime | None:
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
    dt = _parse_date_string(date_value)
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%d")


def _build_type2_row_hints_from_ocr(ocr_pages: list[list[dict]]) -> dict[str, dict[str, str]]:
    if not ocr_pages or not ocr_pages[0]:
        return {}

    lines: dict[int, list[dict]] = {}
    for word in ocr_pages[0]:
        y = round(word["top"] / 8) * 8
        lines.setdefault(y, []).append(word)

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
    for word in top_words:
        y = round(word["top"] / 8) * 8
        lines.setdefault(y, []).append(word)

    line_texts = [
        " ".join(w["text"] for w in sorted(ws, key=lambda z: z["left"]))
        for _, ws in sorted(lines.items())
    ]
    joined = " | ".join(line_texts)

    result: dict[str, str] = {}

    heb_line_re = re.compile(r"[\u0590-\u05FF]{2,}(?:\s+[\u0590-\u05FF]{2,})+")
    for line in line_texts:
        match = heb_line_re.search(line)
        if match:
            candidate = match.group(0).strip()
            if "דוח" not in candidate and "כרטיס" not in candidate:
                result["employee_name"] = candidate
                break

    number_re = re.compile(r"\d{1,4}(?:[\.,]\d{1,2})?")
    pay_match = re.search(r"(?:לתשלום|תשלום)[^\d]{0,10}(\d{1,5}(?:[\.,]\d{1,2})?)", joined)
    rate_match = re.search(r"(?:לשעה|לשעת|מחיר)[^\d]{0,10}(\d{1,4}(?:[\.,]\d{1,2})?)", joined)

    if rate_match:
        result["hour_rate"] = rate_match.group(1).replace(",", ".")
    if pay_match:
        result["payment_total"] = pay_match.group(1).replace(",", ".")

    if "hour_rate" not in result and "payment_total" not in result:
        nums = [m.group(0).replace(",", ".") for m in number_re.finditer(joined)]
        if nums:
            filtered = [n for n in nums if not re.match(r"^(?:[0-2]?\d[\.:][0-5]\d|\d{1,2}/\d{1,2})$", n)]
            if filtered:
                large = [n for n in filtered if float(n) >= 200]
                if large:
                    result.setdefault("payment_total", large[-1])

    return result


def _apply_type2_row_hints(table: ParsedTable, hints: dict[str, dict[str, str]]) -> ParsedTable:
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
