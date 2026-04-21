"""
Unit tests for the pdf_project package.

Tests are grouped by module and deliberately avoid any file I/O or
external binaries (Tesseract / Poppler) so they run offline.
Integration tests that require a real PDF are skipped when no sample is present.
"""
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Helpers from variation module (pure logic, no external deps)
# ---------------------------------------------------------------------------
from pdf_project.variation import (
    _add_durations,
    _duration_str,
    _fmt_time,
    _from_minutes,
    _parse_time,
    _to_minutes,
    Type1Variator,
    Type2Variator,
)
from pdf_project.models import ParsedTable, ReportType, TimeEntry
from pdf_project.classifier import classify
from pdf_project.generator import rtl


# ===========================================================================
# Time utility tests
# ===========================================================================

class TestTimeHelpers(unittest.TestCase):

    def test_parse_time_colon(self):
        self.assertEqual(_parse_time("08:30"), (8, 30))

    def test_parse_time_dot(self):
        self.assertEqual(_parse_time("17.00"), (17, 0))

    def test_parse_time_empty(self):
        self.assertIsNone(_parse_time(""))

    def test_parse_time_garbage(self):
        self.assertIsNone(_parse_time("חופשה"))

    def test_to_from_minutes_roundtrip(self):
        for h in range(0, 24):
            for m in (0, 15, 30, 45):
                self.assertEqual(_from_minutes(_to_minutes(h, m)), (h, m))

    def test_fmt_time(self):
        self.assertEqual(_fmt_time(8, 5), "08:05")
        self.assertEqual(_fmt_time(17, 0), "17:00")

    def test_duration_str(self):
        self.assertEqual(_duration_str(8, 0, 17, 0), "09:00")
        self.assertEqual(_duration_str(8, 30, 17, 15), "08:45")

    def test_add_durations(self):
        self.assertEqual(_add_durations(["08:00", "07:30", "08:30"]), "24:00")
        self.assertEqual(_add_durations([]), "00:00")
        self.assertEqual(_add_durations(["invalid", "08:00"]), "08:00")


# ===========================================================================
# Variation – Type1Variator
# ===========================================================================

def _make_type1_table(entries: list[tuple[str, str, str]]) -> ParsedTable:
    """Build a minimal TYPE_1 ParsedTable from (entry, exit, daily_total) tuples."""
    rows = [
        TimeEntry(
            date=f"0{i+1}/04/2025",
            entry=e,
            exit=x,
            daily_total=d,
        )
        for i, (e, x, d) in enumerate(entries)
    ]
    return ParsedTable(
        headers=["תאריך", "כניסה", "יציאה", 'סה"כ'],
        rows=rows,
        col_map={"date": 0, "entry": 1, "exit": 2, "daily_total": 3},
        metadata={},
    )


class TestType1Variator(unittest.TestCase):

    def setUp(self):
        self.table = _make_type1_table([
            ("08:00", "17:00", "09:00"),
            ("08:15", "17:30", "09:15"),
            ("07:45", "16:45", "09:00"),
        ])
        self.variator = Type1Variator(seed=42)
        self.varied = self.variator.apply(self.table)

    def test_exit_greater_than_entry(self):
        for row in self.varied.rows:
            if row.is_special:
                continue
            entry = _parse_time(row.entry)
            exit_ = _parse_time(row.exit)
            self.assertIsNotNone(entry)
            self.assertIsNotNone(exit_)
            self.assertGreater(
                _to_minutes(*exit_),
                _to_minutes(*entry),
                msg=f"Exit {row.exit} must be after entry {row.entry}",
            )

    def test_daily_total_consistent(self):
        for row in self.varied.rows:
            if row.is_special:
                continue
            entry = _parse_time(row.entry)
            exit_ = _parse_time(row.exit)
            expected = _duration_str(*entry, *exit_)
            self.assertEqual(
                row.daily_total, expected,
                msg=f"daily_total {row.daily_total!r} != expected {expected!r}",
            )

    def test_grand_total_matches_sum(self):
        expected = _add_durations([r.daily_total for r in self.varied.rows])
        self.assertEqual(self.varied.metadata["grand_total"], expected)

    def test_reproducible_with_same_seed(self):
        v2 = Type1Variator(seed=42)
        varied2 = v2.apply(self.table)
        for r1, r2 in zip(self.varied.rows, varied2.rows):
            self.assertEqual(r1.entry, r2.entry)
            self.assertEqual(r1.exit, r2.exit)

    def test_different_seeds_differ(self):
        v2 = Type1Variator(seed=99)
        varied2 = v2.apply(self.table)
        results_42 = [(r.entry, r.exit) for r in self.varied.rows]
        results_99 = [(r.entry, r.exit) for r in varied2.rows]
        self.assertNotEqual(results_42, results_99)

    def test_special_rows_untouched(self):
        table = _make_type1_table([("08:00", "17:00", "09:00")])
        table.rows[0].is_special = True
        table.rows[0].entry = ""
        table.rows[0].exit = ""
        varied = Type1Variator(seed=42).apply(table)
        self.assertEqual(varied.rows[0].entry, "")
        self.assertEqual(varied.rows[0].exit, "")

    def test_entry_within_bounds(self):
        from pdf_project.variation import _ENTRY_MIN, _ENTRY_MAX
        lo = _to_minutes(*_ENTRY_MIN)
        hi = _to_minutes(*_ENTRY_MAX)
        for row in self.varied.rows:
            t = _parse_time(row.entry)
            if t:
                self.assertGreaterEqual(_to_minutes(*t), lo)
                self.assertLessEqual(_to_minutes(*t), hi)

    def test_exit_within_max(self):
        from pdf_project.variation import _EXIT_MAX
        hi = _to_minutes(*_EXIT_MAX)
        for row in self.varied.rows:
            t = _parse_time(row.exit)
            if t:
                self.assertLessEqual(_to_minutes(*t), hi)


# ===========================================================================
# Variation – Type2Variator
# ===========================================================================

class TestType2Variator(unittest.TestCase):

    def _make_table(self) -> ParsedTable:
        rows = [
            TimeEntry(date="01/04/2025", entry="08:00", exit="17:00",
                      daily_total="09:00", employee_name="ישראל ישראלי"),
            TimeEntry(date="02/04/2025", entry="08:15", exit="17:15",
                      daily_total="09:00", employee_name="ישראל ישראלי"),
            TimeEntry(date="01/04/2025", entry="09:00", exit="18:00",
                      daily_total="09:00", employee_name="שרה לוי"),
        ]
        return ParsedTable(
            headers=["שם עובד", "תאריך", "כניסה", "יציאה", 'סה"כ'],
            rows=rows,
            col_map={"employee_name": 0, "date": 1, "entry": 2, "exit": 3, "daily_total": 4},
            metadata={},
        )

    def test_employee_totals_present_in_metadata(self):
        varied = Type2Variator(seed=42).apply(self._make_table())
        self.assertIn("employee_totals", varied.metadata)
        self.assertIn("ישראל ישראלי", varied.metadata["employee_totals"])
        self.assertIn("שרה לוי", varied.metadata["employee_totals"])

    def test_grand_total_equals_sum_of_employee_totals(self):
        varied = Type2Variator(seed=42).apply(self._make_table())
        emp_totals = list(varied.metadata["employee_totals"].values())
        expected = _add_durations(emp_totals)
        self.assertEqual(varied.metadata["grand_total"], expected)


# ===========================================================================
# Classifier
# ===========================================================================

class TestClassifier(unittest.TestCase):

    def test_classify_type1_by_col_map(self):
        table = ParsedTable(col_map={"date": 0, "entry": 1, "exit": 2, "daily_total": 3})
        self.assertEqual(classify(table), ReportType.TYPE_1)

    def test_classify_type2_by_employee_name_col(self):
        table = ParsedTable(col_map={"employee_name": 0, "date": 1, "entry": 2})
        self.assertEqual(classify(table), ReportType.TYPE_2)

    def test_classify_type2_by_heuristic(self):
        rows = [
            TimeEntry(date="01/04", entry="08:00", exit="17:00",
                      daily_total="09:00", employee_name="Alice"),
            TimeEntry(date="01/04", entry="09:00", exit="18:00",
                      daily_total="09:00", employee_name="Bob"),
        ]
        table = ParsedTable(col_map={}, rows=rows)
        self.assertEqual(classify(table), ReportType.TYPE_2)

    def test_classify_type1_fallback(self):
        table = ParsedTable(col_map={}, rows=[])
        self.assertEqual(classify(table), ReportType.TYPE_1)


# ===========================================================================
# RTL helper
# ===========================================================================

class TestRtlHelper(unittest.TestCase):

    def test_non_empty_output(self):
        result = rtl("שלום")
        self.assertTrue(len(result) > 0)

    def test_empty_passthrough(self):
        self.assertEqual(rtl(""), "")

    def test_latin_unchanged(self):
        # Latin-only text should pass through without error
        result = rtl("Hello")
        self.assertIsInstance(result, str)


# ===========================================================================
# Integration test (skipped if no sample PDF present)
# ===========================================================================

_SAMPLE_DIR = Path(__file__).parent.parent / "samples"


class TestIntegration(unittest.TestCase):

    def _find_sample(self) -> Path | None:
        if not _SAMPLE_DIR.exists():
            return None
        pdfs = list(_SAMPLE_DIR.glob("*.pdf"))
        return pdfs[0] if pdfs else None

    @unittest.skipUnless(
        any((_SAMPLE_DIR / "*.pdf").parent.exists() and
            list(_SAMPLE_DIR.glob("*.pdf")) if _SAMPLE_DIR.exists() else []),
        "No sample PDFs found in samples/ directory",
    )
    def test_full_pipeline(self):
        import tempfile, os
        from pdf_project.main import process

        sample = self._find_sample()
        if sample is None:
            self.skipTest("No sample PDF found.")

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            out_path = tmp.name

        try:
            report_type = process(str(sample), out_path, seed=42)
            self.assertIn(report_type, list(ReportType))
            self.assertTrue(Path(out_path).stat().st_size > 0)
        finally:
            os.unlink(out_path)


if __name__ == "__main__":
    unittest.main()
