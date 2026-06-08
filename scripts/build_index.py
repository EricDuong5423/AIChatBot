"""
scripts/build_index.py — Index docs/ → Supabase pgvector (thay Chroma).

Yêu cầu: DATABASE_URL trong .env (postgresql+psycopg://...)
         Supabase đã bật extension: CREATE EXTENSION IF NOT EXISTS vector;

Chạy từ thư mục gốc của project:
    python scripts/build_index.py

Hoặc dùng API: POST /docs/rebuild (background task).
"""

import logging
import os
import sys

# Cho phép import module từ project root khi chạy script từ scripts/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()  # đọc JINA_API_KEY, etc. từ .env trước khi import rag

from rag import build_vectorstore  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

if __name__ == "__main__":
    n = build_vectorstore()
    print(f"\n[ok] Indexed {n} chunks -> Supabase pgvector")
