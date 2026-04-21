"""
Shared data models used across all modules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ReportType(Enum):
    """Supported attendance report layouts."""
    TYPE_1 = "type_1"   # Single-employee monthly report: date | entry | exit | daily_total
    TYPE_2 = "type_2"   # Multi-employee report: name | date | entry | exit | monthly_total


@dataclass
class TimeEntry:
    """One row of attendance data (a single work-day for one employee)."""
    date: str                   # Original display string, e.g. "01/04/2025"
    entry: str                  # Clock-in  time string, e.g. "08:30"
    exit: str                   # Clock-out time string, e.g. "17:00"
    daily_total: str            # Duration string,         e.g. "08:30"
    employee_name: str = ""     # Populated for Type-2 reports
    is_special: bool = False    # True for holidays / sick days / absences (no times to vary)
    raw_row: list[str] = field(default_factory=list)  # Original cell strings (for unknown cols)


@dataclass
class ParsedTable:
    """
    Full structured representation of one attendance report page/section.

    `rows`  – list of TimeEntry objects (one per working day / employee-day).
    `headers` – original Hebrew column headers as extracted from the PDF.
    `col_map` – mapping from internal key to column index in the original table.
    `metadata` – layout hints (column widths, row heights, font sizes, page size,
                 header block text, footer text, etc.) used by the generator to
                 reproduce visual fidelity.
    """
    headers: list[str] = field(default_factory=list)
    rows: list[TimeEntry] = field(default_factory=list)
    col_map: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
