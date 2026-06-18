"""
On-prem DocumentParser: Apache Tika (or 'unstructured') for rich extraction, with a
pypdf / plain-text fallback so the adapter always returns pages even when no Tika
service is reachable.

Backing service (optional): a running Tika server. Preferred path order:
  1. 'unstructured' library partitioning (if installed) -> page-grouped elements.
  2. tika-python -> Apache Tika server (set TIKA_SERVER_ENDPOINT, e.g. http://tika:9998).
  3. pypdf for PDFs / tolerant decode for text (always available; same as the local parser).

native_chunks returns None so CORE's heading-anchored chunk_pages runs uniformly across
every parser backend.

Config (profiles/onprem.yaml):
  tika_url_env: TIKA_URL     -> tika_url: http://tika:9998 (optional; falls back if unset)
"""
from __future__ import annotations

import io
import os
from pathlib import Path

from core.ports.types import Chunk, Page


class TikaParser:
    def __init__(self, tika_url: str = "", **kw):
        # also honor the conventional tika-python env var name.
        self.tika_url = tika_url or os.environ.get("TIKA_SERVER_ENDPOINT", "")

    def supported_types(self) -> set[str]:
        return {".pdf", ".txt", ".md", ".csv", ".docx", ".pptx", ".xlsx", ".html", ".rtf", ".odt"}

    def read_pages(self, data: bytes, filename: str) -> list[Page]:
        suffix = Path(filename).suffix.lower()
        # 1) unstructured (page-aware) if present.
        pages = self._try_unstructured(data, filename)
        if pages:
            return pages
        # 2) Apache Tika server if present.
        pages = self._try_tika(data, filename)
        if pages:
            return pages
        # 3) deterministic fallback.
        if suffix == ".pdf":
            return self._pypdf(data)
        return [Page(page_no=1, text=self._decode(data))]

    def native_chunks(self, pages: list[Page]) -> list[Chunk] | None:
        return None

    # ---- extractors (all SDKs lazy-imported) ----
    def _try_unstructured(self, data: bytes, filename: str) -> list[Page]:
        try:
            from unstructured.partition.auto import partition
        except ImportError:
            return []
        try:
            els = partition(file=io.BytesIO(data), metadata_filename=filename)
        except Exception:
            return []
        by_page: dict[int, list[str]] = {}
        for el in els:
            pno = getattr(getattr(el, "metadata", None), "page_number", None) or 1
            text = str(el).strip()
            if text:
                by_page.setdefault(int(pno), []).append(text)
        if not by_page:
            return []
        return [Page(page_no=p, text="\n".join(by_page[p])) for p in sorted(by_page)]

    def _try_tika(self, data: bytes, filename: str) -> list[Page]:
        if self.tika_url:
            os.environ.setdefault("TIKA_SERVER_ENDPOINT", self.tika_url)
            os.environ.setdefault("TIKA_CLIENT_ONLY", "True")
        try:
            from tika import parser as tika_parser
        except ImportError:
            return []
        try:
            parsed = tika_parser.from_buffer(data)
        except Exception:
            return []
        text = (parsed or {}).get("content") or ""
        text = text.strip()
        if not text:
            return []
        # Tika returns a single content blob; pages are separated by form feeds when present.
        parts = [p for p in text.split("\f")]
        parts = [p for p in parts if p.strip()] or [text]
        return [Page(page_no=i + 1, text=p.strip()) for i, p in enumerate(parts)]

    def _pypdf(self, data: bytes) -> list[Page]:
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(data))
        return [Page(page_no=i + 1, text=p.extract_text() or "") for i, p in enumerate(r.pages)]

    @staticmethod
    def _decode(data: bytes) -> str:
        for enc in ("utf-8-sig", "utf-8", "cp1252"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")
