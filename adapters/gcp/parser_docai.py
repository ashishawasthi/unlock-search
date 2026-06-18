"""
GCP DocumentParser: Document AI Layout Parser (google-cloud-documentai).

Backing service: a Document AI processor (Layout Parser / OCR) in a given project +
location. read_pages() sends the file to the processor and returns one Page per
document page with layout_blocks (paragraphs / headings with bounding info) and
tables extracted from the layout. native_chunks() MAY return layout-aware Chunks
(one per layout block, section = nearest heading) so CORE can skip its char-window
chunker; it returns None when no usable layout is present so CORE chunk_pages runs.

Falls back to pypdf / tolerant text decode when Document AI is unavailable (no
credentials, unsupported type, or the SDK is not installed) so ingest degrades
gracefully instead of failing the upload.

Config / env (config.parser in profiles/gcp.yaml):
  project, location (e.g. "us"), processor_id  (a deployed Layout Parser processor)

Importable without google-cloud-documentai installed (lazy SDK imports).
"""
from __future__ import annotations

import io
from pathlib import Path

from core.ports.types import Chunk, Page

_MIME = {".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/plain",
         ".csv": "text/csv", ".html": "text/html", ".docx":
         "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


class DocAiParser:
    def __init__(self, project: str | None = None, location: str = "us",
                 processor_id: str | None = None, **kw):
        self.project = project
        self.location = location
        self.processor_id = processor_id
        self._client = None

    def supported_types(self) -> set[str]:
        return {".pdf", ".txt", ".md", ".csv", ".html", ".docx"}

    def _docai(self):
        if self._client is None:
            from google.cloud import documentai_v1 as documentai
            from google.api_core.client_options import ClientOptions
            self._documentai = documentai
            opts = ClientOptions(api_endpoint=f"{self.location}-documentai.googleapis.com")
            self._client = documentai.DocumentProcessorServiceClient(client_options=opts)
        return self._client

    def _processor_name(self) -> str:
        return self._client.processor_path(self.project, self.location, self.processor_id)

    def read_pages(self, data: bytes, filename: str) -> list[Page]:
        ext = Path(filename).suffix.lower()
        if not (self.project and self.processor_id):
            return self._fallback_pages(data, filename)
        try:
            client = self._docai()
            documentai = self._documentai
            raw = documentai.RawDocument(content=data, mime_type=_MIME.get(ext, "application/pdf"))
            req = documentai.ProcessRequest(name=self._processor_name(), raw_document=raw)
            doc = client.process_document(request=req).document
        except Exception:
            return self._fallback_pages(data, filename)
        return self._pages_from_doc(doc)

    def _pages_from_doc(self, doc) -> list[Page]:
        full = doc.text or ""

        def slice_text(layout) -> str:
            parts = []
            for seg in layout.text_anchor.text_segments:
                parts.append(full[int(seg.start_index):int(seg.end_index)])
            return "".join(parts).strip()

        pages: list[Page] = []
        for i, p in enumerate(doc.pages):
            blocks = []
            for para in p.paragraphs:
                t = slice_text(para.layout)
                if t:
                    blocks.append({"type": "paragraph", "text": t})
            # headings from detected blocks not already captured as paragraphs
            for blk in p.blocks:
                t = slice_text(blk.layout)
                if t and not any(b["text"] == t for b in blocks):
                    blocks.append({"type": "block", "text": t})
            tables = []
            for tbl in p.tables:
                rows = []
                for row in list(tbl.header_rows) + list(tbl.body_rows):
                    rows.append([slice_text(cell.layout) for cell in row.cells])
                tables.append({"rows": rows})
            page_text = "\n".join(b["text"] for b in blocks)
            pages.append(Page(page_no=p.page_number or (i + 1), text=page_text or "",
                              layout_blocks=blocks or None, tables=tables or None))
        return pages or [Page(page_no=1, text=full)]

    def native_chunks(self, pages: list[Page]) -> list[Chunk] | None:
        """One layout-aware chunk per paragraph block; section = nearest preceding
        short block (heading heuristic). Returns None when no layout is available so
        CORE's chunk_pages runs instead."""
        if not any(p.layout_blocks for p in pages):
            return None
        out: list[Chunk] = []
        seq = 0
        for p in pages:
            section = ""
            for b in (p.layout_blocks or []):
                text = (b.get("text") or "").strip()
                if not text:
                    continue
                if len(text) <= 80 and text.endswith(":") is False and "\n" not in text:
                    section = text  # short standalone line -> treat as section heading
                out.append(Chunk(doc_id="", page_no=p.page_no, chunk_seq=seq,
                                 section=section or "", content=text, chunk_id=None))
                seq += 1
        return out or None

    # ---- fallback (no Document AI) ----
    def _fallback_pages(self, data: bytes, filename: str) -> list[Page]:
        if Path(filename).suffix.lower() == ".pdf":
            try:
                from pypdf import PdfReader
                r = PdfReader(io.BytesIO(data))
                return [Page(page_no=i + 1, text=pg.extract_text() or "")
                        for i, pg in enumerate(r.pages)]
            except Exception:
                pass
        return [Page(page_no=1, text=self._decode(data))]

    @staticmethod
    def _decode(data: bytes) -> str:
        for enc in ("utf-8-sig", "utf-8", "cp1252"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")
