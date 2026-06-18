"""Local DocumentParser: pypdf for PDF, tolerant decode for text. Returns None from
native_chunks so CORE's heading-anchored chunk_pages runs."""
from __future__ import annotations

import io
from pathlib import Path

from core.ports.types import Page


class PypdfParser:
    def __init__(self, **kw):
        pass

    def supported_types(self) -> set[str]:
        return {".pdf", ".txt", ".md", ".csv"}

    def read_pages(self, data: bytes, filename: str) -> list[Page]:
        if Path(filename).suffix.lower() == ".pdf":
            from pypdf import PdfReader
            r = PdfReader(io.BytesIO(data))
            return [Page(page_no=i + 1, text=p.extract_text() or "") for i, p in enumerate(r.pages)]
        return [Page(page_no=1, text=self._decode(data))]

    def native_chunks(self, pages):
        return None

    @staticmethod
    def _decode(data: bytes) -> str:
        for enc in ("utf-8-sig", "utf-8", "cp1252"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")
