# pdf_project – Attendance Report Variation Generator

Reads a scanned Hebrew attendance report PDF, applies a deterministic set of
logical time-shift rules, and produces a new PDF that mirrors the original in
structure and style.

---

## Project structure

```
src/pdf_project/
├── models.py       # ParsedTable, AttendanceRow/TimeEntry, ReportType
├── ocr.py          # PDFScanner  – rasterise + OCR
├── extractor.py    # TableExtractor – grid detection + cell mapping
├── classifier.py   # classify()  – detect report type
├── variation.py    # BaseVariator, Type1Variator, Type2Variator
├── generator.py    # ReportBuilder, rtl()
├── main.py         # cli(), process()  (orchestration)
└── assets/
    ├── FrankRuhlLibre-Regular.ttf
    └── FrankRuhlLibre-Bold.ttf
tests/
└── test_main.py
```

---

## Prerequisites

### 1 – Python

Python 3.8 or newer.

### 2 – Tesseract OCR + Hebrew language pack

**Windows (recommended):**

Download the installer from https://github.com/UB-Mannheim/tesseract/wiki and run it.
During installation tick **Hebrew** under "Additional language data".

Verify:
```
tesseract --version
tesseract --list-langs   # must include "heb"
```

**Linux (Ubuntu/Debian):**
```bash
sudo apt install tesseract-ocr tesseract-ocr-heb
```

### 3 – Poppler (PDF → image rasteriser)

**Windows:**
Download from https://github.com/oschwartz10612/poppler-windows/releases,
extract, and add the `bin\` folder to your `PATH`, **or** pass `--poppler <path>` to the CLI.

**Linux:**
```bash
sudo apt install poppler-utils
```

---

## Installation

```bash
# Clone / unzip the project, then:
cd "pdf files"
pip install -e .
```

This installs all Python dependencies and registers the `pdf-vary` command.

---

## Usage

### Command line

```bash
pdf-vary --input report.pdf --output varied.pdf
```

**Full options:**

```
pdf-vary --input    INPUT        Path to the source scanned PDF
         --output   OUTPUT       Destination path for the varied PDF
         [--seed    SEED]        Integer seed for reproducibility (default: 42)
         [--type    auto|1|2]    Force report type or auto-detect (default: auto)
         [--tesseract PATH]      Path to tesseract.exe (if not on PATH)
         [--poppler  PATH]       Path to poppler bin/ directory (if not on PATH)
         [--verbose]             Enable debug logging
```

**Example with explicit paths (Windows):**

```bash
pdf-vary \
  --input  "C:\reports\april_report.pdf" \
  --output "C:\reports\april_report_varied.pdf" \
  --seed   123 \
  --tesseract "C:\Program Files\Tesseract-OCR\tesseract.exe" \
  --poppler   "C:\poppler\bin"
```

### Programmatic

```python
from pdf_project.main import process
from pdf_project.models import ReportType

report_type = process(
    input_path="report.pdf",
    output_path="varied.pdf",
    seed=42,                    # same seed → same output every run
    report_type="auto",         # or "1" / "2"
)
print(report_type)              # ReportType.TYPE_1 or ReportType.TYPE_2
```

---

## Report types

| Type | Description | Key columns |
|------|-------------|-------------|
| **TYPE_1** | Single-employee monthly report | date · entry · exit · daily_total |
| **TYPE_2** | Multi-employee / department report | employee_name · date · entry · exit · monthly_total |

The classifier detects the type automatically from column headers.
Use `--type 1` or `--type 2` to override.

---

## Variation rules

All randomness is seeded (`random.Random(seed)`) so the same seed always
produces the same output.

| Rule | Detail |
|------|--------|
| Entry time shift | ± 0–15 min, clamped to **06:30 – 10:00** |
| Exit time shift  | ± 0–15 min, enforces **exit ≥ entry + 5 h** and **exit ≤ 20:00** |
| Daily total      | Recalculated from new entry/exit |
| Monthly total    | Recalculated as sum of daily totals |
| Grand total      | Recalculated (TYPE_2: sum of all employee monthly totals) |
| Special rows     | Holidays / absences / weekends left unchanged |

---

## Running tests

```bash
python -m pytest tests/ -v
```

Place sample PDFs in a `samples/` directory at the project root to enable
the integration test.

---

## Hebrew font

The bundled font **FrankRuhlLibre** is licensed under the Open Font Licence (OFL).
Source: https://fonts.google.com/specimen/Frank+Ruhl+Libre
