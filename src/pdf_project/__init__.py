"""
pdf_project – Attendance report PDF variation generator.
"""
from .models import ParsedTable, ReportType, TimeEntry
from .classifier import classify
from .generator import rtl

# Heavy pipeline imports are deferred to avoid loading OCR/table-detection libs
# at import time.  Import explicitly when needed:
#   from pdf_project.main import process, VARIATOR_MAP

__all__ = [
    "ParsedTable",
    "ReportType",
    "TimeEntry",
    "classify",
    "rtl",
]
