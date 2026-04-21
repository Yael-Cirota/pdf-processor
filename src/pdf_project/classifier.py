"""
Report type classifier.

Classification order (first match wins)
----------------------------------------
Step 1 – Fingerprint match (learned from sample PDFs via `pdf-learn`)
    Compares the extracted headers against stored fingerprints in
    assets/fingerprints.json.  Three levels of strictness:
      a) Exact header list match
      b) ≥80% header-token overlap + same column count
      c) ≥60% keyword overlap + same column count

Step 2 – col_map key check (rule-based)
    If col_map contains "employee_name"  → TYPE_2
    If col_map contains "date" + "entry" → TYPE_1

Step 3 – Structural heuristic
    Count unique non-empty employee_name values across rows.
    > 1 unique name → TYPE_2

Step 4 – Default fallback → TYPE_1
"""
from __future__ import annotations

import logging
from pathlib import Path

from .models import ParsedTable, ReportType

logger = logging.getLogger(__name__)

_FINGERPRINTS_PATH = Path(__file__).parent / "assets" / "fingerprints.json"


def classify(table: ParsedTable) -> ReportType:
    """
    Return the ReportType for *table*.

    Parameters
    ----------
    table : a ParsedTable produced by TableExtractor.extract().
    """
    # Step 1 – fingerprint match (only if fingerprints.json exists)
    from .learner import load_fingerprints, match_fingerprint  # deferred import
    fingerprints = load_fingerprints(_FINGERPRINTS_PATH)
    if fingerprints:
        matched = match_fingerprint(table.headers, len(table.headers), fingerprints)
        if matched is not None:
            logger.info("Classifier → %s (fingerprint match)", matched.value)
            return matched
        logger.debug("No fingerprint match; falling back to rule-based classification.")

    col_map = table.col_map

    # Step 2 – column key presence
    if "employee_name" in col_map:
        logger.info("Classifier → TYPE_2 (employee_name column detected)")
        return ReportType.TYPE_2

    if "date" in col_map and "entry" in col_map:
        logger.info("Classifier → TYPE_1 (date + entry columns detected)")
        return ReportType.TYPE_1

    # Step 3 – structural heuristic
    unique_names = {
        row.employee_name.strip()
        for row in table.rows
        if row.employee_name.strip()
    }
    if len(unique_names) > 1:
        logger.info(
            "Classifier → TYPE_2 (heuristic: %d unique employee names)", len(unique_names)
        )
        return ReportType.TYPE_2

    logger.info("Classifier → TYPE_1 (default fallback)")
    return ReportType.TYPE_1
