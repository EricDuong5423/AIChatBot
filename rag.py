"""
rag.py — RAG module: load docs/ → embed → Supabase pgvector → retrieve.

Public API:
    build_vectorstore()            — index lại toàn bộ docs/ → Supabase pgvector
    query_vectorstore(query, k=3)  — search top-k chunks liên quan

Embedding: Jina v3 API (multilingual, 1024D) — gọi REST, không load model local.
Retrieval: hybrid BM25 (keyword) + dense (Jina) merge bằng RRF.
Vector store: Supabase pgvector (persistent, không mất khi Render restart).
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

import requests
from langchain_postgres import PGVector
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from rank_bm25 import BM25Okapi

# ── Cấu hình ────────────────────────────────────────────────────────────────
DOCS_DIR = "docs"
COLLECTION = "hcmut"
# postgresql+psycopg://user:pass@db.xxx.supabase.co:5432/postgres
DATABASE_URL = os.getenv("DATABASE_URL", "")
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

# BM25 cache — invalidate bằng version counter (tăng sau mỗi lần build_vectorstore)
_bm25: Optional[BM25Okapi] = None
_bm25_chunks: list[Document] = []
_bm25_version: int = 0        # tăng sau mỗi rebuild
_bm25_built_version: int = -1  # version lúc build BM25 cache


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
    Rebuild pgvector collection từ docs/ → Supabase.
    pre_delete_collection=True: xóa toàn bộ embeddings cũ rồi reindex.
    Trả về số chunk đã index.
    """
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL chưa set — thêm vào .env trước khi build.")

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

    logger.info("Uploading %d chunks to Supabase pgvector...", len(chunks))
    PGVector.from_documents(
        documents=chunks,
        embedding=_get_embeddings(),
        collection_name=COLLECTION,
        connection=DATABASE_URL,
        pre_delete_collection=True,
        use_jsonb=True,
    )
    logger.info("Indexed %d chunks -> Supabase pgvector (collection=%s)", len(chunks), COLLECTION)

    # Invalidate BM25 cache
    global _bm25, _bm25_chunks, _bm25_version
    _bm25 = None
    _bm25_chunks = []
    _bm25_version += 1

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
    """
    Build BM25 index từ chunks trong Supabase. Cache trong memory.
    Fetch raw text từ langchain_pg_embedding table qua psycopg (không cần re-embed).
    """
    global _bm25, _bm25_chunks, _bm25_built_version, _bm25_version

    if _bm25 is not None and _bm25_version == _bm25_built_version:
        return _bm25, _bm25_chunks  # cache valid

    if not DATABASE_URL:
        return None, []

    import psycopg

    # psycopg.connect dùng postgresql:// (không có +psycopg prefix của SQLAlchemy)
    plain_url = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")

    try:
        logger.info("Building BM25 index from Supabase chunks...")
        with psycopg.connect(plain_url) as conn:
            rows = conn.execute("""
                SELECT e.document, e.cmetadata
                FROM langchain_pg_embedding e
                JOIN langchain_pg_collection c ON e.collection_id = c.uuid
                WHERE c.name = %s
            """, (COLLECTION,)).fetchall()
    except Exception as e:
        logger.warning("BM25 load from Supabase failed: %s", e)
        return None, []

    chunks: list[Document] = [
        Document(page_content=row[0], metadata=row[1] or {}) for row in rows
    ]
    if not chunks:
        return None, []

    tokenized = [_tokenize_vi(c.page_content) for c in chunks]
    _bm25 = BM25Okapi(tokenized)
    _bm25_chunks = chunks
    _bm25_built_version = _bm25_version
    logger.info("BM25 ready: %d chunks tokenized", len(chunks))
    return _bm25, _bm25_chunks


def query_vectorstore(query: str, k: int = 3) -> list[dict]:
    """
    Hybrid search: BM25 (keyword) + dense embedding (semantic), merge bằng RRF.
    Bù điểm yếu của semantic-only cho tiếng Việt + keyword đặc thù.
    """
    if not DATABASE_URL:
        logger.warning("DATABASE_URL chưa set — RAG disabled")
        return []

    # Lấy top-k từ semantic search (pgvector HNSW)
    try:
        store = PGVector(
            embeddings=_get_embeddings(),
            collection_name=COLLECTION,
            connection=DATABASE_URL,
        )
        vec_results = store.similarity_search(query, k=k * 2)  # over-fetch để merge
    except Exception as e:
        logger.warning("PGVector search failed: %s", e)
        return []

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
