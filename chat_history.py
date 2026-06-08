"""
chat_history.py — Đọc/ghi lịch sử trò chuyện từ Supabase chatbox table.

Schema (do C# backend / Supabase tạo):
    public.history(id, header, create_date, user_id)
        -- một phiên trò chuyện, thuộc về 1 user
    public.chatbox(id, content, contact_time, contact_person, history_id)
        -- từng tin nhắn trong phiên, contact_person = 'user' | 'assistant'

Flow:
    C# tạo history record, lấy history_id → gửi lên Python /chat kèm history_id
    Python fetch chatbox theo history_id → pass vào Pipecat pipeline như history[]
    Python save user message + assistant response trở lại chatbox
    C# chỉ cần truyền history_id, không cần quản lý history array
"""

import logging
import os

logger = logging.getLogger(__name__)


def _plain_url() -> str:
    return os.getenv("DATABASE_URL", "").replace("postgresql+psycopg://", "postgresql://")


def fetch_history(history_id: str, limit: int = 20) -> list[dict]:
    """
    Lấy `limit` tin nhắn MỚI NHẤT của phiên `history_id`, trả về theo thứ tự
    cũ → mới (chronological) để feed vào LLM context.

    Subquery DESC + LIMIT lấy n tin gần nhất, outer ASC sắp lại đúng thứ tự hội thoại.
    Nếu chỉ ORDER BY ASC + LIMIT sẽ lấy nhầm n tin ĐẦU TIÊN (mất context gần đây).

    contact_person được normalize: "user" → "user", còn lại → "assistant"
    (khớp logic C# AskAsync, vì Pipecat chỉ nhận role "user"|"assistant").
    """
    import psycopg

    plain_url = _plain_url()
    if not plain_url or not history_id:
        return []

    try:
        with psycopg.connect(plain_url) as conn:
            rows = conn.execute(
                """
                SELECT contact_person, content FROM (
                    SELECT contact_person, content, contact_time
                    FROM public.chatbox
                    WHERE history_id = %s
                    ORDER BY contact_time DESC NULLS LAST
                    LIMIT %s
                ) recent
                ORDER BY contact_time ASC NULLS FIRST
                """,
                (history_id, limit),
            ).fetchall()
        return [
            {"role": "user" if row[0] == "user" else "assistant", "content": row[1]}
            for row in rows
        ]
    except Exception as e:
        logger.warning("fetch_history(%s) failed: %s", history_id, e)
        return []


def save_exchange(history_id: str, user_content: str, assistant_content: str) -> None:
    """
    Lưu cặp (user message, assistant response) vào chatbox.
    assistant_content: text phản hồi hoặc "[Dẫn đường: <dest>]" nếu navigation.
    """
    import psycopg

    plain_url = _plain_url()
    if not plain_url or not history_id:
        return

    try:
        with psycopg.connect(plain_url) as conn:
            conn.execute(
                """
                INSERT INTO public.chatbox (content, contact_time, contact_person, history_id)
                VALUES
                    (%s, NOW(),                            'user',      %s),
                    (%s, NOW() + INTERVAL '1 millisecond', 'assistant', %s)
                """,
                (user_content, history_id, assistant_content, history_id),
            )
            conn.commit()
    except Exception as e:
        logger.warning("save_exchange(%s) failed: %s", history_id, e)
