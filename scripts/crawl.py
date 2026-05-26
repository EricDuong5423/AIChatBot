"""
scripts/crawl.py — Crawl SPA sites bằng Playwright (render JS thật).

hcmut.edu.vn là SPA — body shell rỗng, content load bằng JS sau khi page load.
Playwright launch headless Chromium, đợi JS render xong, rồi extract HTML đã render.

Chạy từ thư mục gốc (chỉ cần khi muốn refresh data từ SPA — site static dùng UI /docs/crawl-url):
    python scripts/crawl.py

Đầu ra: docs/{slug}.md với YAML frontmatter (source_url + crawled_at).
"""

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

import html2text
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# ── Cấu hình ────────────────────────────────────────────────────────────────
# Routes thật của SPA hcmut.edu.vn (discovered bằng cách render homepage và lấy anchor href).
# /gioi-thieu, /dao-tao là prefix — không phải route. Phải dùng sub-path.
SEED_URLS = [
    "https://hcmut.edu.vn/",
    "https://hcmut.edu.vn/gioi-thieu/ke-hoach-chien-luoc",
    "https://hcmut.edu.vn/gioi-thieu/xep-hang-dai-hoc",
    "https://hcmut.edu.vn/gioi-thieu/thanh-tuu-kiem-dinh",
    "https://hcmut.edu.vn/co-cau-to-chuc",
    "https://hcmut.edu.vn/dao-tao/chuong-trinh-dao-tao",
    "https://hcmut.edu.vn/dao-tao/hoc-phi",
    "https://hcmut.edu.vn/dao-tao/quy-che-quy-dinh",
    "https://hcmut.edu.vn/dao-tao/lich-hoc-vu",
    "https://hcmut.edu.vn/ho-so-nang-luc",
    "https://hcmut.edu.vn/ktxbk",
    "https://hcmut.edu.vn/library-page",
    "https://hcmut.edu.vn/ky-nang-va-nghe-nghiep",
    "https://hcmut.edu.vn/hop-tac/du-an-quoc-te",
]

DOCS_DIR = "docs"
PAGE_TIMEOUT_MS = 30_000        # 30s cho page.goto
WAIT_AFTER_LOAD_MS = 2_500      # đợi thêm sau DOMContentLoaded để JS render xong
MIN_CONTENT_LEN = 200
CONCURRENCY = 3                 # crawl 3 page song song (browser context riêng)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def url_to_slug(url: str) -> str:
    """URL → slug an toàn cho filesystem."""
    parsed = urlparse(url)
    parts = [parsed.netloc.replace(".", "_")]
    if parsed.path and parsed.path != "/":
        parts.append(parsed.path.strip("/").replace("/", "_"))
    else:
        parts.append("home")
    slug = "_".join(parts)
    return re.sub(r"[^a-zA-Z0-9_-]", "_", slug).lower()


def extract_main_content(html: str) -> str:
    """Loại nav/footer/script. Tìm container chính. HTML → markdown text."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript", "iframe"]):
        tag.decompose()

    main = None
    for selector in ["main", "article", "[role=main]", ".content", "#content", ".main-content", "#app"]:
        found = soup.select_one(selector)
        if found and len(found.get_text(strip=True)) > 100:
            main = found
            break
    if main is None:
        main = soup.body or soup

    converter = html2text.HTML2Text()
    converter.ignore_links = False
    converter.ignore_images = True
    converter.body_width = 0
    text = converter.handle(str(main))

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def save_doc(slug: str, url: str, text: str) -> str:
    os.makedirs(DOCS_DIR, exist_ok=True)
    path = os.path.join(DOCS_DIR, f"{slug}.md")
    crawled = datetime.now(timezone.utc).isoformat()
    body = (
        f"---\n"
        f"source_url: {url}\n"
        f"crawled_at: {crawled}\n"
        f"---\n\n"
        f"{text}\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


async def crawl_one(browser, url: str) -> tuple[str, str | int | None]:
    """
    Render 1 URL → save file.
    Trả về (url, path) nếu success, (url, error_msg) nếu fail.
    """
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        locale="vi-VN",
    )
    page = await context.new_page()
    try:
        try:
            await page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        except Exception as e:
            return url, f"goto error: {e}"

        # Đợi JS app render xong. Thử networkidle, nếu không thì rơi về timeout cố định.
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        await page.wait_for_timeout(WAIT_AFTER_LOAD_MS)

        html = await page.content()
        text = extract_main_content(html)
        if len(text) < MIN_CONTENT_LEN:
            return url, f"content too short ({len(text)} chars)"

        slug = url_to_slug(url)
        path = save_doc(slug, url, text)
        return url, path
    finally:
        await context.close()


async def crawl_all() -> tuple[list, list]:
    """Launch browser, crawl all URLs với concurrency limit."""
    success: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        sem = asyncio.Semaphore(CONCURRENCY)

        async def bounded(url):
            async with sem:
                logger.info("-> Crawling %s", url)
                result = await crawl_one(browser, url)
                url_, payload = result
                if payload and str(payload).startswith("docs/"):
                    success.append((url_, payload))
                    logger.info("   saved -> %s", payload)
                else:
                    failed.append((url_, str(payload)))
                    logger.warning("   FAIL %s — %s", url_, payload)
                return result

        await asyncio.gather(*(bounded(u) for u in SEED_URLS))
        await browser.close()

    return success, failed


def main():
    success, failed = asyncio.run(crawl_all())

    print("\n" + "=" * 60)
    print(f"Crawl summary: {len(success)} success, {len(failed)} failed")
    print("=" * 60)
    for url, path in success:
        try:
            n = os.path.getsize(path)
        except OSError:
            n = 0
        print(f"  [ok]   {url} -> {path} ({n} bytes)")
    for url, reason in failed:
        print(f"  [fail] {url} - {reason}")

    if len(success) < 2:
        print("\nCrawl được < 2 page. Paste vài file .md vào docs/ rồi chạy build_index.py.")
        sys.exit(1)


if __name__ == "__main__":
    main()
