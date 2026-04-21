"""
Learner – builds structural fingerprints from labelled sample PDFs.

Workflow
--------
1. Place samples in the samples/ directory named:
       type1_<anything>.pdf   ← will be fingerprinted as TYPE_1
       type2_<anything>.pdf   ← will be fingerprinted as TYPE_2

2. Run:
       pdf-learn --samples samples/ --output src/pdf_project/assets/fingerprints.json

3. The generated fingerprints.json is then used automatically by the classifier
   (exact structural match before falling back to rule-based detection).

Fingerprint structure (one entry per sample)
--------------------------------------------
{
  "source":     "type1_april.pdf",
  "report_type": "type_1",
  "col_count":  5,
  "headers":    ["תאריך", "כניסה", "יציאה", "סה\"כ", "הערות"],
  "header_set": ["תאריך", "כניסה", "יציאה"],   ← normalised, order-independent
  "col_widths": [120, 90, 90, 90, 110],          ← pixel widths from img2table
  "keywords":   ["כניסה", "יציאה", "תאריך"]     ← high-frequency OCR tokens
}
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from .models import ReportType

logger = logging.getLogger(__name__)

_DEFAULT_FINGERPRINTS_PATH = Path(__file__).parent / "assets" / "fingerprints.json"


class Learner:
    """
    Scans labelled sample PDFs and writes a fingerprints JSON file.

    Parameters
    ----------
    tesseract_cmd : optional path to Tesseract binary.
    poppler_path  : optional path to Poppler bin directory.
    """

    def __init__(
        self,
        tesseract_cmd: Optional[str] = None,
        poppler_path: Optional[str] = None,
    ):
        # Deferred imports: cv2/pytesseract/pdf2image only loaded when Learner is used
        from .ocr import PDFScanner          # noqa: PLC0415
        from .extractor import TableExtractor  # noqa: PLC0415
        self._scanner = PDFScanner(tesseract_cmd=tesseract_cmd, poppler_path=poppler_path)
        self._extractor = TableExtractor()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def learn(
        self,
        samples_dir: str | Path,
        output_path: str | Path = _DEFAULT_FINGERPRINTS_PATH,
    ) -> list[dict]:
        """
        Process every PDF in *samples_dir* whose name starts with 'type1_' or
        'type2_', build a fingerprint for each, and save them to *output_path*.

        Returns the list of fingerprint dicts that were saved.
        """
        samples_dir = Path(samples_dir)
        output_path = Path(output_path)

        pdfs = list(samples_dir.glob("*.pdf"))
        if not pdfs:
            raise FileNotFoundError(f"No PDF files found in {samples_dir}")

        fingerprints: list[dict] = []

        for pdf in sorted(pdfs):
            report_type = self._infer_type_from_filename(pdf.name)
            if report_type is None:
                logger.warning(
                    "Skipping %s – filename must start with 'type1_' or 'type2_'",
                    pdf.name,
                )
                continue

            logger.info("Learning from %s (→ %s) …", pdf.name, report_type.value)
            fingerprint = self._fingerprint_pdf(pdf, report_type)
            fingerprints.append(fingerprint)
            logger.info(
                "  headers=%s  col_count=%d",
                fingerprint["headers"],
                fingerprint["col_count"],
            )

        if not fingerprints:
            raise ValueError(
                "No labelled samples processed. "
                "Rename files as type1_<name>.pdf or type2_<name>.pdf."
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(fingerprints, f, ensure_ascii=False, indent=2)

        logger.info("Saved %d fingerprint(s) to %s", len(fingerprints), output_path)
        return fingerprints

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fingerprint_pdf(self, pdf_path: Path, report_type: ReportType) -> dict:
        """OCR and extract the first table; build a fingerprint dict."""
        images = self._scanner.pdf_to_images(pdf_path)
        ocr_pages = [
            self._scanner.ocr_page(self._scanner.preprocess(img))
            for img in images
        ]
        table = self._extractor.extract(images, ocr_pages)

        headers = table.headers
        col_widths = table.metadata.get("col_widths", [])

        # keyword_set: all unique non-empty header tokens (whitespace-split)
        # Used for partial/fuzzy matching when exact header list differs slightly
        header_set = list({
            token.strip()
            for h in headers
            for token in h.split()
            if token.strip()
        })

        # High-frequency OCR tokens across *all* words on page 0 (layout hint)
        keywords = _top_tokens(ocr_pages[0] if ocr_pages else [], top_n=20)

        return {
            "source":      pdf_path.name,
            "report_type": report_type.value,
            "col_count":   len(headers),
            "headers":     headers,
            "header_set":  header_set,
            "col_widths":  col_widths,
            "keywords":    keywords,
        }

    @staticmethod
    def _infer_type_from_filename(filename: str) -> Optional[ReportType]:
        """Return ReportType based on 'type1_' or 'type2_' filename prefix."""
        lower = filename.lower()
        if lower.startswith("type1_"):
            return ReportType.TYPE_1
        if lower.startswith("type2_"):
            return ReportType.TYPE_2
        return None


# ---------------------------------------------------------------------------
# Public helpers used by classifier.py
# ---------------------------------------------------------------------------

def load_fingerprints(
    path: str | Path = _DEFAULT_FINGERPRINTS_PATH,
) -> list[dict]:
    """Load fingerprints from *path*. Returns [] if the file does not exist."""
    path = Path(path)
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def match_fingerprint(
    headers: list[str],
    col_count: int,
    fingerprints: list[dict],
) -> Optional[ReportType]:
    """
    Find the best-matching fingerprint for the given headers and column count.

    Matching strategy (first match wins, strictest first):
    1. Exact header list match.
    2. Exact col_count + full header_set subset match (≥80% overlap).
    3. col_count match + majority keyword overlap (≥60%).

    Returns the matched ReportType, or None if no fingerprint matches.
    """
    if not fingerprints:
        return None

    query_set = {
        token.strip()
        for h in headers
        for token in h.split()
        if token.strip()
    }

    best_score = 0.0
    best_type: Optional[ReportType] = None

    for fp in fingerprints:
        fp_type = ReportType(fp["report_type"])

        # --- Strategy 1: exact header list ---
        if headers == fp["headers"] and col_count == fp["col_count"]:
            return fp_type

        # --- Strategy 2: header_set overlap ---
        fp_set = set(fp.get("header_set", []))
        if fp_set and query_set:
            overlap = len(query_set & fp_set) / max(len(fp_set), len(query_set))
            if col_count == fp["col_count"] and overlap >= 0.80:
                if overlap > best_score:
                    best_score = overlap
                    best_type = fp_type

        # --- Strategy 3: keyword overlap (looser fallback) ---
        fp_kw = set(fp.get("keywords", []))
        if fp_kw and query_set:
            kw_overlap = len(query_set & fp_kw) / max(len(fp_kw), 1)
            score = kw_overlap * 0.5   # weight lower than header overlap
            if col_count == fp["col_count"] and kw_overlap >= 0.60:
                if score > best_score:
                    best_score = score
                    best_type = fp_type

    return best_type


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------

def _top_tokens(words: list[dict], top_n: int = 20) -> list[str]:
    """Return the *top_n* most frequent non-numeric tokens from OCR word list."""
    from collections import Counter
    counts: Counter = Counter()
    for w in words:
        text = w.get("text", "").strip()
        if text and not text.isdigit() and len(text) > 1:
            counts[text] += 1
    return [token for token, _ in counts.most_common(top_n)]
