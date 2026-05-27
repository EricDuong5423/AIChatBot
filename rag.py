"""
rag.py — RAG module: load docs/ → embed → Chroma vector store → retrieve.

Public API:
    build_vectorstore()            — index lại toàn bộ docs/ → chroma_db/
    query_vectorstore(query, k=3)  — search top-k chunks liên quan

Embedding: Jina v3 API (multilingual, 1024D) — gọi REST, không load model local.
Retrieval: hybrid BM25 (keyword) + dense (Jina) merge bằng RRF.
Vector store: Chroma persistent ở thư mục ./chroma_db/.
"""

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Optional

import requests
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from rank_bm25 import BM25Okapi

# ── Cấu hình ────────────────────────────────────────────────────────────────
DOCS_DIR = "docs"
CHROMA_DIR = "chroma_db"
COLLECTION = "hcmut"
# Jina v3 — multilingual API embedding, 1024D. Free 1M token/tháng tại jina.ai.
# Lý do dùng API thay vì local model: Render free tier 512MB RAM không đủ
# cho HF model (~540MB). Jina API: 0 RAM local, chất lượng MTEB top.
EMBED_MODEL = "jina-embeddings-v3"
EMBED_DIM = 1024  # Jina v3 default dimension
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50

logger = logging.getLogger(__name__)


# ============================================================================
# JINA EMBEDDINGS — REST API client, conforms to LangChain Embeddings interface
# ============================================================================

class JinaEmbeddings(Embeddings):
    """
    Jina v3 multilingual embedding via REST API.
    - Endpoint: https://api.jina.ai/v1/embeddings
    - Task `retrieval.passage` cho documents, `retrieval.query` cho query
      (Jina v3 tách 2 mode để optimize asymmetric retrieval)
    - Batch tối đa 32 texts/request để tránh hit rate limit
    """

    API_URL = "https://api.jina.ai/v1/embeddings"
    BATCH_SIZE = 32

    def __init__(self, api_key: str, model: str = EMBED_MODEL):
        if not api_key:
            raise RuntimeError("JINA_API_KEY chưa set — đăng ký free tại jina.ai")
        self.api_key = api_key
        self.model = model

    def _call(self, texts: list[str], task: str) -> list[list[float]]:
        results: list[list[float]] = []
        for i in range(0, len(texts), self.BATCH_SIZE):
            batch = texts[i : i + self.BATCH_SIZE]
            resp = requests.post(
                self.API_URL,
                json={"model": self.model, "task": task, "input": batch},
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
            results.extend(item["embedding"] for item in data)
        return results

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._call(texts, task="retrieval.passage")

    def embed_query(self, text: str) -> list[float]:
        return self._call([text], task="retrieval.query")[0]


_embeddings: Optional[JinaEmbeddings] = None

# BM25 cache: build từ chunks hiện tại của Chroma. Invalidate khi build_vectorstore.
_bm25: Optional[BM25Okapi] = None
_bm25_chunks: list[Document] = []
_bm25_built_at: float = 0.0  # mtime của chroma_db để detect rebuild


def _get_embeddings() -> JinaEmbeddings:
    """Lazy init Jina embedder. Đọc JINA_API_KEY từ env."""
    global _embeddings
    if _embeddings is None:
        api_key = os.getenv("JINA_API_KEY", "").strip()
        logger.info("Initializing Jina embedder: %s", EMBED_MODEL)
        _embeddings = JinaEmbeddings(api_key=api_key, model=EMBED_MODEL)
    return _embeddings


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML-style frontmatter ở đầu file. Trả về (metadata_dict, body)."""
    metadata: dict = {}
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", content, re.DOTALL)
    if not m:
        return metadata, content
    raw, body = m.group(1), m.group(2)
    for line in raw.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            metadata[k.strip()] = v.strip()
    return metadata, body.lstrip()


def _load_docs() -> list[Document]:
    """
    Đọc tất cả docs/**/*.md và docs/**/*.txt, parse frontmatter (chỉ .md).
    .txt: giữ nguyên content. Files trong docs/uploaded/ được mark `is_uploaded=true`
    để query_vectorstore boost chúng trong RRF (user-curated > crawled noise).
    """
    docs: list[Document] = []
    docs_path = Path(DOCS_DIR)
    if not docs_path.exists():
        logger.warning("Folder %s không tồn tại", DOCS_DIR)
        return docs

    for path in sorted(docs_path.rglob("*")):
        if path.suffix not in (".md", ".txt") or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".md":
            meta, body = _parse_frontmatter(text)
        else:
            meta, body = {}, text
        if not body.strip():
            continue
        meta["filename"] = path.name
        # Mark uploaded sources cho boost retrieval
        if "uploaded" in str(path.parent):
            meta["is_uploaded"] = "true"
        docs.append(Document(page_content=body, metadata=meta))
    return docs


# ============================================================================
# PUBLIC API
# ============================================================================

def build_vectorstore() -> int:
    """
    Rebuild Chroma DB từ docs/. Clear collection (qua Chroma API) thay vì xóa thư mục
    — để hoạt động ngay cả khi có connection SQLite đang mở (warmup, ongoing query, etc.).
    Trả về số chunk đã index.
    """
    docs = _load_docs()
    if not docs:
        raise RuntimeError(f"Không tìm thấy doc nào trong {DOCS_DIR}/ — upload file hoặc chạy scripts/crawl.py trước.")

    logger.info("Loaded %d documents from %s/", len(docs), DOCS_DIR)

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    chunks = splitter.split_documents(docs)
    logger.info("Split into %d chunks (chunk_size=%d, overlap=%d)",
                len(chunks), CHUNK_SIZE, CHUNK_OVERLAP)

    # Mở store hiện có (hoặc tạo mới) — KHÔNG xóa filesystem
    store = Chroma(
        persist_directory=CHROMA_DIR,
        collection_name=COLLECTION,
        embedding_function=_get_embeddings(),
    )

    # Clear toàn bộ documents cũ trong collection
    try:
        existing = store._collection.get(include=[])  # chỉ lấy ids
        if existing.get("ids"):
            store._collection.delete(ids=existing["ids"])
            logger.info("Cleared %d old chunks from collection", len(existing["ids"]))
    except Exception as e:
        logger.warning("Clear collection error (ignored): %s", e)

    # Thêm chunks mới
    store.add_documents(chunks)
    logger.info("Indexed %d chunks -> %s/", len(chunks), CHROMA_DIR)

    # Invalidate BM25 cache để build lại ở query đầu tiên
    global _bm25, _bm25_chunks, _bm25_built_at
    _bm25 = None
    _bm25_chunks = []
    _bm25_built_at = 0.0

    return len(chunks)


def _tokenize_vi(text: str) -> list[str]:
    """
    Tokenize tiếng Việt đơn giản: lowercase + split theo whitespace + punctuation.
    Không dùng underthesea/pyvi vì nặng và không cần thiết cho BM25 word-level matching.
    """
    text = text.lower()
    # Tách punctuation, giữ alphanumeric + dấu tiếng Việt
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return [t for t in text.split() if len(t) > 1]


def _load_bm25() -> tuple[Optional[BM25Okapi], list[Document]]:
    """Build BM25 index từ chunks trong Chroma. Cache trong memory."""
    global _bm25, _bm25_chunks, _bm25_built_at

    if not os.path.exists(CHROMA_DIR):
        return None, []

    # Check rebuild detector: mtime of chroma sqlite
    sqlite_path = os.path.join(CHROMA_DIR, "chroma.sqlite3")
    current_mtime = os.path.getmtime(sqlite_path) if os.path.exists(sqlite_path) else 0.0

    if _bm25 is not None and current_mtime == _bm25_built_at:
        return _bm25, _bm25_chunks  # cache valid

    logger.info("Building BM25 index from Chroma chunks…")
    store = Chroma(
        persist_directory=CHROMA_DIR,
        collection_name=COLLECTION,
        embedding_function=_get_embeddings(),
    )
    result = store._collection.get()
    docs = result.get("documents", [])
    metas = result.get("metadatas", [])

    chunks: list[Document] = [
        Document(page_content=d, metadata=m or {}) for d, m in zip(docs, metas)
    ]
    if not chunks:
        return None, []

    tokenized = [_tokenize_vi(c.page_content) for c in chunks]
    _bm25 = BM25Okapi(tokenized)
    _bm25_chunks = chunks
    _bm25_built_at = current_mtime
    logger.info("BM25 ready: %d chunks tokenized", len(chunks))
    return _bm25, _bm25_chunks


def query_vectorstore(query: str, k: int = 3) -> list[dict]:
    """
    Hybrid search: BM25 (keyword) + dense embedding (semantic), merge bằng RRF.
    Bù điểm yếu của semantic-only cho tiếng Việt + keyword đặc thù.
    """
    if not os.path.exists(CHROMA_DIR):
        logger.warning("Chroma DB chưa tồn tại — chạy scripts/build_index.py hoặc upload doc trước")
        return []

    # Lấy top-k từ semantic search
    store = Chroma(
        persist_directory=CHROMA_DIR,
        collection_name=COLLECTION,
        embedding_function=_get_embeddings(),
    )
    vec_results = store.similarity_search(query, k=k * 2)  # over-fetch để merge

    # Lấy top-k từ BM25
    bm25, bm25_chunks = _load_bm25()
    bm25_results: list[Document] = []
    if bm25 is not None and bm25_chunks:
        q_tokens = _tokenize_vi(query)
        if q_tokens:
            scores = bm25.get_scores(q_tokens)
            # Top-k indices
            top_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[: k * 2]
            bm25_results = [bm25_chunks[i] for i in top_idx if scores[i] > 0]

    # Reciprocal Rank Fusion (RRF): score = sum(weight / (60 + rank))
    # Weights:
    #   - User-uploaded: ×2.5 (curated, ưu tiên cao)
    #   - "Link-only" chunks (>40% là markdown link [text](url) hoặc URL): ×0.4
    #     để chunks toàn Drive links từ crawled docs không lên top.
    K_RRF = 60
    UPLOAD_BOOST = 2.5
    LINK_PENALTY = 0.4
    scores_map: dict[str, float] = {}
    doc_map: dict[str, Document] = {}

    def _weight(doc: Document) -> float:
        w = 1.0
        if doc.metadata.get("is_uploaded") == "true":
            w *= UPLOAD_BOOST
        # Heuristic: nếu phần lớn nội dung là links/URLs → đây là chunk nội dung kém
        content = doc.page_content
        if len(content) > 50:
            # Đếm ký tự nằm trong [text](url) hoặc URL bare
            link_chars = sum(len(m) for m in re.findall(r"\[[^\]]*\]\([^)]+\)|https?://\S+", content))
            if link_chars / len(content) > 0.4:
                w *= LINK_PENALTY
        return w

    for rank, doc in enumerate(vec_results):
        key = doc.page_content[:200]
        scores_map[key] = scores_map.get(key, 0.0) + _weight(doc) / (K_RRF + rank)
        doc_map[key] = doc
    for rank, doc in enumerate(bm25_results):
        key = doc.page_content[:200]
        scores_map[key] = scores_map.get(key, 0.0) + _weight(doc) / (K_RRF + rank)
        doc_map.setdefault(key, doc)

    # Sort by RRF score, take top-k
    sorted_keys = sorted(scores_map.keys(), key=lambda x: scores_map[x], reverse=True)[:k]

    return [
        {
            "content": doc_map[key].page_content,
            "source_url": doc_map[key].metadata.get("source_url"),
            "filename": doc_map[key].metadata.get("filename"),
        }
        for key in sorted_keys
    ]
