"""
Stage 1 — Pre-Processing & Layout Anchoring.

Converts PDF pages to high-resolution images (300 DPI) and optionally
runs a fast layout-detection pass to identify Regions of Interest (ROIs).

Uses **PyMuPDF (fitz)** for PDF→image conversion — lightweight, no
system-level dependencies (unlike poppler-based pdf2image).
"""

import base64
import io
import logging
from typing import List, Tuple

from PIL import Image

from src.config import DPI, MAX_PAGES

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# PDF → Images
# ────────────────────────────────────────────────────────────────────

def pdf_to_images(pdf_bytes: bytes, dpi: int = DPI) -> List[str]:
    """
    Convert a PDF to a list of base64-encoded PNG images (one per page).

    Returns at most ``MAX_PAGES`` pages.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise ImportError(
            "PyMuPDF is required for PDF processing. "
            "Install it with: pip install pymupdf"
        ) from exc

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page_count = min(len(doc), MAX_PAGES)
    logger.info("PDF has %d pages (processing %d)", len(doc), page_count)

    images_b64: List[str] = []
    zoom = dpi / 72.0  # 72 DPI is the PDF default
    matrix = fitz.Matrix(zoom, zoom)

    for idx in range(page_count):
        page = doc[idx]
        pix = page.get_pixmap(matrix=matrix, alpha=False)

        # Convert pixmap → PNG bytes → base64
        img_bytes = pix.tobytes("png")
        images_b64.append(base64.b64encode(img_bytes).decode("utf-8"))
        logger.debug("Page %d rendered (%dx%d)", idx + 1, pix.width, pix.height)

    doc.close()
    return images_b64


# ────────────────────────────────────────────────────────────────────
# Image → base64 (for pre-uploaded images)
# ────────────────────────────────────────────────────────────────────

def image_to_b64(image_bytes: bytes) -> str:
    """
    Normalise an image (PNG/JPEG/TIFF/WebP) to a base64-encoded PNG.

    We ensure the image is in RGB mode and not excessively large.
    """
    img = Image.open(io.BytesIO(image_bytes))

    # Handle RGBA → RGB
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGB")

    # Cap resolution to avoid memory blow-ups
    max_dim = 4096
    if max(img.size) > max_dim:
        ratio = max_dim / max(img.size)
        new_size = (int(img.width * ratio), int(img.height * ratio))
        img = img.resize(new_size, Image.LANCZOS)
        logger.info("Resized image to %s", new_size)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# ────────────────────────────────────────────────────────────────────
# Foveal Re-scan (2× resolution crop for audit recovery)
# ────────────────────────────────────────────────────────────────────

def foveal_crop(
    page_image_b64: str,
    bbox_pct: Tuple[float, float, float, float],
    scale: float = 2.0,
) -> str:
    """
    Crop and upscale a region from a page image for re-scanning.

    ``bbox_pct`` is (x%, y%, w%, h%) relative to the full page.
    Returns a base64-encoded PNG of the cropped region at ``scale``× resolution.
    """
    img_bytes = base64.b64decode(page_image_b64)
    img = Image.open(io.BytesIO(img_bytes))

    x_pct, y_pct, w_pct, h_pct = bbox_pct
    left = int(img.width * x_pct / 100.0)
    top = int(img.height * y_pct / 100.0)
    right = int(img.width * (x_pct + w_pct) / 100.0)
    bottom = int(img.height * (y_pct + h_pct) / 100.0)

    # Clamp to image bounds
    left, top = max(0, left), max(0, top)
    right = min(img.width, right)
    bottom = min(img.height, bottom)

    crop = img.crop((left, top, right, bottom))

    # Upscale
    new_size = (int(crop.width * scale), int(crop.height * scale))
    crop = crop.resize(new_size, Image.LANCZOS)

    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")
