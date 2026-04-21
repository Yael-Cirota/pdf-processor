"""
OCR module – converts a scanned PDF into per-page word-level data.

External requirements
---------------------
* Tesseract OCR binary   https://github.com/UB-Mannheim/tesseract/wiki
* Hebrew tessdata pack   (heb.traineddata in the Tesseract tessdata folder)
* Poppler binaries       https://github.com/oschwartz10612/poppler-windows/releases
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import pytesseract
from pdf2image import convert_from_path
from PIL import Image
from pytesseract import Output

logger = logging.getLogger(__name__)

# Tesseract language string: Hebrew + English digits / Latin headers
_OCR_LANG = "heb+eng"
# DPI used for rasterising PDF pages – 300 is the OCR sweet-spot for text
_DPI = 300


class PDFScanner:
    """
    Converts a scanned PDF to word-level OCR data.

    Usage
    -----
    scanner = PDFScanner()
    pages   = scanner.ocr_pdf("attendance.pdf")
    # pages[0] is a list of word dicts for the first page
    """

    def __init__(self, tesseract_cmd: Optional[str] = None, poppler_path: Optional[str] = None):
        """
        Parameters
        ----------
        tesseract_cmd : path to the tesseract executable (optional; uses PATH by default).
        poppler_path  : path to the poppler bin folder     (optional; uses PATH by default).
        """
        if tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        self._poppler_path = poppler_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def pdf_to_images(self, path: str | Path) -> list[Image.Image]:
        """Rasterise every page of *path* into a PIL Image at 300 DPI."""
        images = convert_from_path(
            str(path),
            dpi=_DPI,
            poppler_path=self._poppler_path,
        )
        logger.info("Rasterised %d page(s) from %s", len(images), path)
        return images

    def preprocess(self, image: Image.Image) -> Image.Image:
        """
        Prepare a page image for OCR:
        1. Convert to grayscale.
        2. Apply Otsu binarisation (improves OCR accuracy on scans).
        3. Deskew if the page is slightly rotated.
        """
        img = np.array(image.convert("RGB"))
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        deskewed = self._deskew(binary)
        return Image.fromarray(deskewed)

    def ocr_page(self, image: Image.Image) -> list[dict]:
        """
        Run Tesseract on a single preprocessed PIL image.

        Returns a list of word-level dicts with keys:
            text, left, top, width, height, conf
        Empty / low-confidence tokens are filtered out.
        """
        data = pytesseract.image_to_data(
            image,
            lang=_OCR_LANG,
            output_type=Output.DICT,
            config="--psm 6",   # Assume a single uniform block of text
        )
        words = []
        for i, word in enumerate(data["text"]):
            text = word.strip()
            if not text:
                continue
            conf = int(data["conf"][i])
            if conf < 20:        # Discard very low-confidence tokens
                continue
            words.append({
                "text":   text,
                "left":   data["left"][i],
                "top":    data["top"][i],
                "width":  data["width"][i],
                "height": data["height"][i],
                "conf":   conf,
            })
        return words

    def ocr_pdf(self, path: str | Path) -> list[list[dict]]:
        """
        Full pipeline: rasterise → preprocess → OCR for every page.

        Returns one list of word dicts per page.
        """
        images = self.pdf_to_images(path)
        results = []
        for idx, img in enumerate(images):
            logger.debug("OCR-ing page %d / %d …", idx + 1, len(images))
            preprocessed = self.preprocess(img)
            words = self.ocr_page(preprocessed)
            results.append(words)
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _deskew(gray: np.ndarray) -> np.ndarray:
        """
        Straighten a slightly-rotated page via the minimum-area bounding box
        of all foreground pixels.  Skips rotation if the angle is negligible.
        """
        # Invert so text is white on black (needed for minAreaRect)
        inverted = cv2.bitwise_not(gray)
        coords = np.column_stack(np.where(inverted > 0))
        if coords.size == 0:
            return gray
        angle = cv2.minAreaRect(coords)[-1]
        # minAreaRect returns angles in [-90, 0); convert to a small rotation
        if angle < -45:
            angle = 90 + angle
        if abs(angle) < 0.5:    # Not worth rotating
            return gray
        h, w = gray.shape
        centre = (w // 2, h // 2)
        M = cv2.getRotationMatrix2D(centre, angle, 1.0)
        rotated = cv2.warpAffine(
            gray, M, (w, h),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )
        return rotated
