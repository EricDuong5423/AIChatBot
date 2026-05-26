"""
routers/docs.py — Quản lý knowledge base (docs/) qua HTTP.

Endpoints (yêu cầu X-API-Key):
  GET    /docs              — liệt kê file đã upload (filename, size, mtime)
  POST   /docs/upload       — upload .md/.txt/.pdf, lưu vào docs/uploaded/
  DELETE /docs/{filename}   — xóa file trong docs/uploaded/
  POST   /docs/rebuild      — re-index Chroma (background task)

PDF được auto-extract text bằng pypdf rồi lưu thành .md.
.md/.txt giữ nguyên.
"""

import asyncio
import io
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import html2text
import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Security, UploadFile
from pydantic import BaseModel, Field
from pypdf import PdfReader

from auth import verify_api_key
from rag import build_vectorstore

router = APIRouter(prefix="/docs", tags=["docs"])
logger = logging.getLogger(__name__)

UPLOAD_DIR = Path("docs/uploaded")
ALLOWED_EXTS = {".md", ".txt", ".pdf"}
MAX_SIZE_MB = 20
URL_FETCH_TIMEOUT = 15
URL_MIN_CONTENT_CHARS = 200  # < threshold → coi như fail (SPA hoặc trang rỗng)
URL_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Trạng thái rebuild (đơn giản: 1 flag in-memory) — đủ cho single-instance deploy.
_rebuild_state: dict = {"running": False, "last_at": None, "last_chunks": None, "last_error": None}


def _safe_filename(name: str) -> str:
    """Loại ký tự nguy hiểm path traversal. Giữ chữ + số + . _ - ()."""
    base = Path(name).name  # bỏ mọi component path
    return re.sub(r"[^A-Za-z0-9._\-() ]", "_", base).strip() or "file"


def _extract_pdf_text(content: bytes) -> str:
    """Trích text từ PDF bytes. Mỗi page join bằng 2 newline."""
    reader = PdfReader(io.BytesIO(content))
    pages = []
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception as e:
            logger.warning("PDF page %d extract fail: %s", i, e)
            text = ""
        if text.strip():
            pages.append(text.strip())
    return "\n\n".join(pages)


def _fetch_and_extract(url: str) -> str:
    """
    GET URL, parse HTML, extract main content thành markdown text.
    Sync — gọi qua asyncio.to_thread. Không xử lý SPA (cần JS render).
    """
    resp = requests.get(
        url,
        headers={"User-Agent": URL_USER_AGENT, "Accept-Language": "vi,en;q=0.9"},
        timeout=URL_FETCH_TIMEOUT,
        allow_redirects=True,
    )
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript", "iframe"]):
        tag.decompose()

    main = None
    for selector in ["main", "article", "[role=main]", ".content", "#content", ".main-content"]:
        found = soup.select_one(selector)
        if found and len(found.get_text(strip=True)) > 100:
            main = found
            break
    if main is None:
        main = soup.body or soup

    conv = html2text.HTML2Text()
    conv.ignore_links = False
    conv.ignore_images = True
    conv.body_width = 0
    text = conv.handle(str(main))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _url_to_slug(url: str) -> str:
    """URL → file-safe slug (~80 chars max)."""
    parsed = urlparse(url)
    base = (parsed.netloc + parsed.path).replace("/", "_").replace(".", "_").strip("_") or "page"
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", base).lower()
    return safe[:80] or "page"


def _save_uploaded(filename: str, content: str, ext: str, source: str = "user_upload") -> Path:
    """
    Lưu file upload, giữ extension gốc:
      - .md: thêm YAML frontmatter (source, uploaded_at) để metadata vào RAG
      - .txt: lưu CONTENT NGUYÊN, không thêm frontmatter (giữ format gốc)
    .pdf qua handler khác — extract text rồi save làm .txt.
    """
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOAD_DIR / f"{filename}{ext}"
    if ext == ".md":
        crawled = datetime.now(timezone.utc).isoformat()
        body = (
            f"---\n"
            f"source_url: upload://{filename}\n"
            f"source: {source}\n"
            f"uploaded_at: {crawled}\n"
            f"---\n\n"
            f"{content.strip()}\n"
        )
        path.write_text(body, encoding="utf-8")
    else:
        # .txt — giữ nguyên content, không modify
        path.write_text(content, encoding="utf-8")
    return path


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.get("")
async def list_uploaded():
    """List file trong docs/uploaded/. Public — anyone can xem KB hiện có."""
    if not UPLOAD_DIR.exists():
        return []
    items = []
    for p in sorted(UPLOAD_DIR.iterdir()):
        if p.suffix not in (".md", ".txt"):
            continue
        st = p.stat()
        items.append({
            "filename": p.name,
            "size_bytes": st.st_size,
            "modified_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        })
    return items


@router.post("/upload", dependencies=[Security(verify_api_key)])
async def upload_doc(
    file: UploadFile = File(...),
    background_tasks: BackgroundTasks = None,
    rebuild: bool = True,
):
    """
    Upload .md/.txt/.pdf. Mặc định trigger re-index background sau upload.
    Truyền rebuild=false để skip (upload nhiều file rồi rebuild 1 lần ở cuối).
    """
    if not file.filename:
        raise HTTPException(400, "filename rỗng")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(400, f"Chỉ chấp nhận {sorted(ALLOWED_EXTS)}")

    content = await file.read()
    if len(content) > MAX_SIZE_MB * 1024 * 1024:
        raise HTTPException(413, f"File quá lớn (> {MAX_SIZE_MB} MB)")
    if not content:
        raise HTTPException(400, "File rỗng")

    safe = _safe_filename(Path(file.filename).stem)

    try:
        if ext == ".pdf":
            text = _extract_pdf_text(content)
            if not text.strip():
                raise HTTPException(422, "PDF không có text trích được (có thể là PDF ảnh scan)")
            # PDF binary không index được → extract text rồi save .txt
            path = _save_uploaded(safe, text, ".txt", source=f"pdf:{file.filename}")
        elif ext == ".txt":
            text = content.decode("utf-8", errors="replace")
            # .txt giữ nguyên format gốc, không thêm frontmatter
            path = _save_uploaded(safe, text, ".txt", source=f"upload:{file.filename}")
        else:  # .md
            text = content.decode("utf-8", errors="replace")
            path = _save_uploaded(safe, text, ".md", source=f"upload:{file.filename}")
    except UnicodeDecodeError:
        raise HTTPException(400, "File không phải UTF-8 text hợp lệ")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Upload xử lý lỗi")
        raise HTTPException(500, f"Xử lý file lỗi: {e}")

    response = {
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "extracted_chars": len(text),
        "rebuild_scheduled": rebuild,
    }

    if rebuild and background_tasks is not None:
        background_tasks.add_task(_rebuild_index_safe)

    return response


class CrawlUrlRequest(BaseModel):
    url: str = Field(..., description="URL bắt đầu http(s):// để crawl")
    rebuild: bool = Field(True, description="True = trigger rebuild background sau khi crawl")


@router.post("/crawl-url", dependencies=[Security(verify_api_key)])
async def crawl_url(body: CrawlUrlRequest, background_tasks: BackgroundTasks):
    """
    Fetch URL → trích content chính → lưu vào docs/uploaded/ → rebuild background.
    Chỉ hoạt động với static HTML site (requests + BeautifulSoup).
    SPA cần JS render: dùng script `python scripts/crawl.py` local rồi upload .md.
    """
    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "URL phải bắt đầu bằng http:// hoặc https://")

    try:
        text = await asyncio.to_thread(_fetch_and_extract, url)
    except requests.HTTPError as e:
        raise HTTPException(502, f"HTTP error từ URL: {e}")
    except requests.RequestException as e:
        raise HTTPException(502, f"Fetch URL lỗi: {e}")
    except Exception as e:
        logger.exception("crawl-url xử lý lỗi")
        raise HTTPException(500, f"Lỗi không xác định: {e}")

    if len(text) < URL_MIN_CONTENT_CHARS:
        raise HTTPException(
            422,
            f"Chỉ extract được {len(text)} ký tự (< {URL_MIN_CONTENT_CHARS}). "
            "Trang có thể là SPA (JS render) — request thấy body rỗng. "
            "Hãy dùng `python scripts/crawl.py` (Playwright headless browser) local "
            "rồi upload file .md kết quả ở mục Upload File.",
        )

    slug = _url_to_slug(url)
    # URL crawl: kết quả là markdown (do html2text), save .md với frontmatter
    path = _save_uploaded(slug, text, ".md", source=f"url:{url}")

    if body.rebuild:
        background_tasks.add_task(_rebuild_index_safe)

    return {
        "filename": path.name,
        "source_url": url,
        "extracted_chars": len(text),
        "rebuild_scheduled": body.rebuild,
    }


@router.delete("/{filename}", dependencies=[Security(verify_api_key)])
async def delete_doc(filename: str, background_tasks: BackgroundTasks, rebuild: bool = True):
    """Xóa file trong docs/uploaded/. Trigger rebuild sau."""
    safe = _safe_filename(filename)
    path = UPLOAD_DIR / safe
    if not path.exists():
        raise HTTPException(404, f"Không tìm thấy {safe}")
    path.unlink()
    if rebuild:
        background_tasks.add_task(_rebuild_index_safe)
    return {"deleted": safe, "rebuild_scheduled": rebuild}


@router.post("/rebuild", dependencies=[Security(verify_api_key)])
async def trigger_rebuild(background_tasks: BackgroundTasks):
    """Trigger re-index Chroma từ tất cả docs/ (bao gồm uploaded). Chạy background."""
    if _rebuild_state["running"]:
        return {"status": "already_running", "state": _rebuild_state}
    background_tasks.add_task(_rebuild_index_safe)
    return {"status": "scheduled", "state": _rebuild_state}


@router.get("/rebuild/status")
async def rebuild_status():
    """Xem trạng thái rebuild gần nhất."""
    return _rebuild_state


# ============================================================================
# INTERNAL
# ============================================================================

async def _rebuild_index_safe() -> None:
    """Chạy build_vectorstore() trong thread, ghi state vào _rebuild_state."""
    if _rebuild_state["running"]:
        logger.info("Rebuild đã đang chạy, skip")
        return
    _rebuild_state["running"] = True
    _rebuild_state["last_error"] = None
    try:
        n = await asyncio.to_thread(build_vectorstore)
        _rebuild_state["last_chunks"] = n
        _rebuild_state["last_at"] = datetime.now(timezone.utc).isoformat()
        logger.info("Rebuild done: %d chunks", n)
    except Exception as e:
        logger.exception("Rebuild lỗi")
        _rebuild_state["last_error"] = str(e)[:300]
    finally:
        _rebuild_state["running"] = False
