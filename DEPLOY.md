# Deploy lên Render

## 1. Push code lên GitHub

```bash
git add .
git commit -m "Initial chatbot deploy"
git push
```

## 2. Tạo Render service

1. Vào https://dashboard.render.com → **New +** → **Blueprint**
2. Connect repo GitHub vừa push
3. Render detect `render.yaml` → click **Apply**

`render.yaml` đã set sẵn:
- Python 3.12
- Build: `pip install -r requirements.txt` + `npm ci && npm run build`
- Start: `uvicorn api:app --host 0.0.0.0 --port $PORT`
- Hầu hết env vars (model, base URL, timeout, CORS, plan free)

## 3. Set 2 secrets (Render dashboard)

Vào **Environment** tab của service, set 2 biến `sync: false`:

| Key | Giá trị |
|---|---|
| `LLM_API_KEY` | Groq API key (lấy tại https://console.groq.com) |
| `CHATBOT_API_KEY` | Random string, dùng `openssl rand -hex 32` để generate |

(Optional) `CHATBOT_ALLOWED_ORIGINS`: domain frontend public, ngăn cách bằng dấu phẩy.
Mặc định `*` (cho phép tất cả) — set hẹp hơn cho production.

## 4. Click Deploy

Lần đầu deploy mất **~5-10 phút** vì:
- Cài Python deps (~2 phút)
- `npm ci && npm run build` (~2 phút)
- Khi server start, HF embedding model (~500MB) được download từ HuggingFace Hub (~3 phút)
- Auto-build Chroma index từ `docs/` (~10 giây)

Sau lần đầu, các deploy tiếp theo nhanh hơn (~2-3 phút) — embed model được cache trong Render container layer.

## 5. Test

Mở URL service Render cung cấp:
- `/` — Vue dashboard (Chat Test + Knowledge Base tabs)
- `/health` — JSON `{"status":"ok"}`
- `/swagger` — FastAPI Swagger UI (test endpoints)

Nhập `CHATBOT_API_KEY` (giá trị bạn vừa set) vào ô **API Key** trên dashboard rồi test chat.

## 6. Quản lý knowledge base

Mở tab **Knowledge Base** trên dashboard:
- **Học từ URL**: paste URL bài viết/wiki (static HTML), backend tự fetch + extract + save
- **Upload** `.md` / `.txt` / `.pdf` (tối đa 20 MB)
- File được lưu vào `docs/uploaded/` trên container
- Index tự rebuild background sau mỗi upload/crawl

**Lưu ý**: `crawl-url` chỉ hoạt động với static HTML page. Site SPA (như hcmut.edu.vn chính) sẽ trả 422 — phải dùng `python scripts/crawl.py` local (cần Playwright + Chromium) rồi upload file `.md` kết quả.

**Cảnh báo Free tier**: Render free plan **KHÔNG có persistent disk**. Mỗi lần service restart/redeploy, mọi file user upload bị mất (vì `docs/uploaded/` và `chroma_db/` không persist). Files trong `docs/` được commit vào git thì vẫn còn.

→ Để giữ uploads qua restart, hoặc:
1. **Commit upload vào git**: download file từ Render, push vào `docs/`, redeploy
2. **Upgrade Starter plan ($7/tháng)** + uncomment block `disk:` trong `render.yaml`:
   ```yaml
   disk:
     name: data
     mountPath: /opt/render/project/src/chroma_db
     sizeGB: 1
   ```
   Tương tự cho `docs/uploaded` nếu cần.

## 7. Troubleshooting

| Triệu chứng | Nguyên nhân | Fix |
|---|---|---|
| "Failed to call a function" 429 | Groq hit daily token limit | Đổi `OLLAMA_MODEL` sang model khác có quota riêng |
| Cold start timeout | Lần deploy đầu, embed model chưa cache | Đợi ~3 phút, hoặc retry |
| Chat trả "không phản hồi kịp 20 giây" | Multi-tool chain quá lâu | Tăng `CHATBOT_TIMEOUT_SECS` lên 30 |
| Upload báo "File quá lớn" | > 20 MB | Tăng `MAX_SIZE_MB` trong `routers/docs.py` |
| Frontend không load | Build frontend lỗi | Check Render build log, có thể thiếu Node phiên bản |

## Cập nhật model

`render.yaml` default model: `meta-llama/llama-4-scout-17b-16e-instruct`.
Các model khác trên Groq (set vào env `OLLAMA_MODEL`):
- `openai/gpt-oss-20b` — nhanh, tool calling chặt
- `llama-3.1-8b-instant` — nhanh nhất, lite
- `llama-3.3-70b-versatile` — chất lượng cao nhưng tool calling đôi khi fail

Đổi model không cần redeploy code — chỉ cần update env var rồi Render auto-restart.
