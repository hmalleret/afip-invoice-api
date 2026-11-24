"""
PDF text and image extractor.
Uses pdfplumber for digital text and pdftoppm for scanned/rasterized PDF rendering.
Scans for AFIP QR codes across all pages.
"""

from __future__ import annotations

import base64
import io
import os
import subprocess
import tempfile
from typing import Optional

import pdfplumber
from PIL import Image

from .qr_extractor import scan_afip_qr


class PDFExtractionResult:
    def __init__(
        self,
        text: str,
        is_scanned: bool,
        page_images: list[Image.Image],
        qr_data: Optional[dict] = None,
    ):
        self.text = text
        self.is_scanned = is_scanned
        self.page_images = page_images
        self.qr_data = qr_data


def process_pdf(content: str | bytes) -> PDFExtractionResult:
    """
    Process PDF:
    1. Extract selectable text from pages.
    2. Render pages to images with pdftoppm.
    3. Scan pages for AFIP QR codes.
    4. If no selectable text is found, mark as scanned and return images.
    """
    if isinstance(content, str):
        try:
            raw_bytes = base64.b64decode(content)
        except Exception as exc:
            raise ValueError(f"Content is not valid base64: {exc}") from exc
    else:
        raw_bytes = content

    if len(raw_bytes) == 0:
        raise ValueError("Empty PDF content")

    # 1. Try text extraction with pdfplumber
    pages_text: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            for i, page in enumerate(pdf.pages):
                txt = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
                tables = page.extract_tables()
                if tables:
                    for table in tables:
                        for row in table:
                            if row:
                                row_str = " | ".join(str(c or "").strip() for c in row)
                                if row_str.strip():
                                    txt += f"\n{row_str}"
                if txt.strip():
                    pages_text.append(f"[Página {i+1}]\n{txt.strip()}")
    except Exception as exc:
        raise ValueError(f"Could not open PDF: {exc}") from exc

    full_text = "\n\n".join(pages_text)
    is_scanned = len(full_text.strip()) == 0

    # 2. Render pages with pdftoppm to images (for QR scanning and scanned fallback)
    page_images: list[Image.Image] = []
    qr_data: Optional[dict] = None

    with tempfile.TemporaryDirectory() as tmpdir:
        input_pdf = os.path.join(tmpdir, "doc.pdf")
        with open(input_pdf, "wb") as f:
            f.write(raw_bytes)

        prefix = os.path.join(tmpdir, "page")
        try:
            subprocess.run(
                ["pdftoppm", "-png", "-r", "150", input_pdf, prefix],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            rendered_files = sorted(
                [os.path.join(tmpdir, f) for f in os.listdir(tmpdir) if f.startswith("page-") and f.endswith(".png")]
            )
            for img_path in rendered_files:
                img = Image.open(img_path).convert("RGB")
                page_images.append(img.copy())

                if qr_data is None:
                    found_qr = scan_afip_qr(img)
                    if found_qr:
                        qr_data = found_qr
        except Exception:
            pass

    return PDFExtractionResult(
        text=full_text,
        is_scanned=is_scanned,
        page_images=page_images,
        qr_data=qr_data,
    )


# Backward-compatible helper
def extract_text_from_pdf(content: str | bytes) -> str:
    res = process_pdf(content)
    if res.is_scanned and not res.page_images:
        raise ValueError(
            "PDF contains no extractable text and could not be rendered as image."
        )
    return res.text
