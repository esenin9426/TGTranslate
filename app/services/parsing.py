"""Get plain text (and a title) out of a web page or a PDF file."""

from dataclasses import dataclass
from pathlib import Path

import trafilatura
from pypdf import PdfReader


@dataclass
class Extracted:
    title: str
    text: str


def _clean(text: str) -> str:
    """Postgres text columns cannot hold NUL characters, which PDF extraction sometimes produces."""
    return text.replace("\x00", "")


def extract_html(html: bytes | str, url: str) -> Extracted:
    """Extract the article from already downloaded HTML (see services.fetching for the download)."""
    text = trafilatura.extract(html)
    if not text:
        raise ValueError("No article text found on the page")
    meta = trafilatura.extract_metadata(html)
    return Extracted(title=_clean(meta.title if meta and meta.title else url), text=_clean(text))


def extract_pdf(path: str) -> Extracted:
    try:
        reader = PdfReader(path)
        text = _clean("\n".join(page.extract_text() or "" for page in reader.pages))
    except Exception as exc:  # pypdf raises many different errors on damaged or encrypted files
        raise ValueError("Could not read the PDF (it may be damaged or password-protected)") from exc
    if not text.strip():
        raise ValueError("No text layer in the PDF (scanned books need OCR)")
    title = (reader.metadata.title if reader.metadata else None) or Path(path).stem
    return Extracted(title=_clean(str(title)), text=text)
