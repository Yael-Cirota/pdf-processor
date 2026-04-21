"""
Variation engine – applies a deterministic, seed-controlled set of logical
changes to a ParsedTable to produce a credible variation of an attendance report.

Variation rules (applied per working-day row)
---------------------------------------------
1. Entry time  shifted by a random integer in [-15, +15] minutes.
   Clamped to [ENTRY_MIN, ENTRY_MAX] (06:30 – 10:00).

2. Exit time   shifted independently by [-15, +15] minutes.
   Then clamped so that:
     a) exit ≥ entry + MIN_WORK_HOURS   (employees always work ≥ 5 hours)
     b) exit ≤ EXIT_MAX                  (20:00)

3. Daily total recalculated as exit – entry.

4. After all rows are varied:
   - Monthly / weekly subtotal rows are recalculated from the daily totals.
   - Grand-total footer rows are recalculated.

5. Special rows (holidays, absences, weekends) are detected by TimeEntry.is_special
   and left unchanged.

All randomness passes through a single random.Random(seed) instance so the
same seed always produces the same output – fully reproducible.
"""
from __future__ import annotations

import logging
import random
from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import timedelta

from .models import ParsedTable, ReportType, TimeEntry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_ENTRY_MIN = (6, 30)    # (hour, minute)
_ENTRY_MAX = (10, 0)
_EXIT_MAX = (20, 0)
_MIN_WORK_DELTA = timedelta(hours=5)
_SHIFT_RANGE = 15       # ± minutes


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _parse_time(s: str) -> tuple[int, int] | None:
    """Parse 'HH:MM' → (hour, minute), or return None if unparseable."""
    s = s.strip()
    if not s:
        return None
    for sep in (":", ".", "-"):
        if sep in s:
            parts = s.split(sep)
            try:
                return int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                return None
    return None


def _fmt_time(hour: int, minute: int) -> str:
    """Format (hour, minute) → 'HH:MM'."""
    return f"{hour:02d}:{minute:02d}"


def _to_minutes(hour: int, minute: int) -> int:
    return hour * 60 + minute


def _from_minutes(total: int) -> tuple[int, int]:
    return divmod(total, 60)


def _clamp_minutes(value: int, lo: tuple[int, int], hi: tuple[int, int]) -> int:
    return max(_to_minutes(*lo), min(_to_minutes(*hi), value))


def _duration_str(entry_h: int, entry_m: int, exit_h: int, exit_m: int) -> str:
    """Return 'HH:MM' duration string (exit − entry)."""
    delta = _to_minutes(exit_h, exit_m) - _to_minutes(entry_h, entry_m)
    if delta < 0:
        delta = 0
    h, m = divmod(delta, 60)
    return f"{h:02d}:{m:02d}"


def _add_durations(times: list[str]) -> str:
    """Sum a list of 'HH:MM' duration strings and return 'HH:MM'."""
    total = 0
    for t in times:
        parsed = _parse_time(t)
        if parsed:
            total += _to_minutes(*parsed)
    h, m = divmod(total, 60)
    return f"{h:02d}:{m:02d}"


# ---------------------------------------------------------------------------
# Base variator
# ---------------------------------------------------------------------------

class BaseVariator(ABC):
    """Abstract base for all variator strategies."""

    def __init__(self, seed: int = 42):
        self.rng = random.Random(seed)

    @abstractmethod
    def apply(self, table: ParsedTable) -> ParsedTable:
        """Return a new ParsedTable with varied time data."""

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _vary_row(self, row: TimeEntry) -> TimeEntry:
        """
        Apply time-shift variation to a single TimeEntry.
        Returns untouched row if is_special or if times cannot be parsed.
        """
        if row.is_special:
            return row

        entry_t = _parse_time(row.entry)
        exit_t = _parse_time(row.exit)
        if entry_t is None or exit_t is None:
            return row  # Unparseable – leave as-is

        # --- Shift entry ---
        entry_min = _clamp_minutes(
            _to_minutes(*entry_t) + self.rng.randint(-_SHIFT_RANGE, _SHIFT_RANGE),
            _ENTRY_MIN, _ENTRY_MAX,
        )

        # --- Shift exit ---
        exit_min_raw = _to_minutes(*exit_t) + self.rng.randint(-_SHIFT_RANGE, _SHIFT_RANGE)
        # Enforce minimum working duration and maximum exit time
        exit_min = max(exit_min_raw, entry_min + int(_MIN_WORK_DELTA.total_seconds() // 60))
        exit_min = min(exit_min, _to_minutes(*_EXIT_MAX))

        new_entry_t = _from_minutes(entry_min)
        new_exit_t = _from_minutes(exit_min)
        new_daily = _duration_str(*new_entry_t, *new_exit_t)

        return replace(
            row,
            entry=_fmt_time(*new_entry_t),
            exit=_fmt_time(*new_exit_t),
            daily_total=new_daily,
        )


# ---------------------------------------------------------------------------
# Type-1 variator  (single-employee monthly report)
# ---------------------------------------------------------------------------

class Type1Variator(BaseVariator):
    """
    Varies a Type-1 (single-employee) ParsedTable.

    After varying each day's entry/exit, the monthly grand total stored in
    metadata["grand_total"] is recalculated from the updated daily totals.
    """

    def apply(self, table: ParsedTable) -> ParsedTable:
        varied_rows = [self._vary_row(row) for row in table.rows]
        grand_total = _add_durations([r.daily_total for r in varied_rows if not r.is_special])
        new_metadata = {**table.metadata, "grand_total": grand_total}
        logger.info("Type1Variator: grand_total recalculated → %s", grand_total)
        return ParsedTable(
            headers=table.headers,
            rows=varied_rows,
            col_map=table.col_map,
            metadata=new_metadata,
        )


# ---------------------------------------------------------------------------
# Type-2 variator  (multi-employee / department report)
# ---------------------------------------------------------------------------

class Type2Variator(BaseVariator):
    """
    Varies a Type-2 (multi-employee) ParsedTable.

    1. Varies each individual day row.
    2. Recalculates each employee's monthly total.
    3. Recalculates the department grand total (sum of all monthly totals).
    """

    def apply(self, table: ParsedTable) -> ParsedTable:
        # Group rows by employee name preserving insertion order
        employee_rows: dict[str, list[int]] = {}   # name → list of indices
        for idx, row in enumerate(table.rows):
            key = row.employee_name.strip() or f"__unknown_{idx}"
            employee_rows.setdefault(key, []).append(idx)

        varied_rows = list(table.rows)

        # Vary each row and track per-employee totals
        employee_totals: dict[str, str] = {}
        for name, indices in employee_rows.items():
            daily_totals: list[str] = []
            for idx in indices:
                varied = self._vary_row(varied_rows[idx])
                varied_rows[idx] = varied
                if not varied.is_special:
                    daily_totals.append(varied.daily_total)
            employee_totals[name] = _add_durations(daily_totals)
            logger.debug("Type2Variator: %s monthly total → %s", name, employee_totals[name])

        # Recalculate grand total across all employees
        grand_total = _add_durations(list(employee_totals.values()))
        new_metadata = {
            **table.metadata,
            "employee_totals": employee_totals,
            "grand_total": grand_total,
        }
        logger.info("Type2Variator: grand_total recalculated → %s", grand_total)
        return ParsedTable(
            headers=table.headers,
            rows=varied_rows,
            col_map=table.col_map,
            metadata=new_metadata,
        )
