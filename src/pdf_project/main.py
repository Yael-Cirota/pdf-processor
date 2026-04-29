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
import sys
from pathlib import Path

from .classifier import classify
from .extractor import TableExtractor
from .generator import ReportBuilder
from .models import ReportType
from .ocr import PDFScanner
from .variation import BaseVariator, Type1Variator, Type2Variator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Variator registry – extend here to support new report types
# ---------------------------------------------------------------------------
VARIATOR_MAP: dict[ReportType, type[BaseVariator]] = {
    ReportType.TYPE_1: Type1Variator,
    ReportType.TYPE_2: Type2Variator,
}


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

    # Step 4 – Variation
    logger.info("Step 4: Applying variation (seed=%d) …", seed)
    variator_cls = VARIATOR_MAP[detected_type]
    variator = variator_cls(seed=seed)
    varied_table = variator.apply(table)

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
