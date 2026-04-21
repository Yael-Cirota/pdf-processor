"""
CLI entry point for `pdf-learn`.

Usage
-----
    pdf-learn --samples samples/ [--output src/pdf_project/assets/fingerprints.json]

Each PDF in the samples directory must be named:
    type1_<anything>.pdf    ← labelled as TYPE_1
    type2_<anything>.pdf    ← labelled as TYPE_2

The resulting fingerprints.json is used automatically by the classifier
the next time `pdf-vary` is run.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .learner import Learner, _DEFAULT_FINGERPRINTS_PATH


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="pdf-learn",
        description=(
            "Learn structural fingerprints from labelled sample attendance PDFs.\n\n"
            "Name each sample:  type1_<name>.pdf  or  type2_<name>.pdf"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--samples", "-s",
        required=True,
        metavar="DIR",
        help="Directory containing labelled sample PDFs.",
    )
    parser.add_argument(
        "--output", "-o",
        default=str(_DEFAULT_FINGERPRINTS_PATH),
        metavar="FILE",
        help=(
            f"Destination for the fingerprints JSON file "
            f"(default: {_DEFAULT_FINGERPRINTS_PATH})"
        ),
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
        learner = Learner(tesseract_cmd=args.tesseract, poppler_path=args.poppler)
        fingerprints = learner.learn(
            samples_dir=args.samples,
            output_path=args.output,
        )
        print(f"Learned {len(fingerprints)} fingerprint(s) → {args.output}")
        for fp in fingerprints:
            print(f"  [{fp['report_type']}]  {fp['source']}  ({fp['col_count']} columns)")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()
