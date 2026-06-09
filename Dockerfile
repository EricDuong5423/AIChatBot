# ─────────────────────────────────────────────────────────────────────────────
# STAGE 1 — Build frontend Vue (vite outDir = ../dist → /app/dist)
# ─────────────────────────────────────────────────────────────────────────────
FROM node:20-slim AS frontend

WORKDIR /app/frontend

# Cache layer: chỉ cài lại deps khi package.json đổi
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
# vite.config.js có build.outDir = '../dist' → output ra /app/dist
RUN npm run build


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 2 — Python runtime (FastAPI + Pipecat). LLM/embedding/DB đều ở cloud
# nên image gọn, không cần torch/CUDA.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Không ghi .pyc, log unbuffered (thấy log realtime trong docker logs)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Cache layer: chỉ cài lại pip khi requirements.txt đổi
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source backend (.dockerignore đã loại .env, .venv, node_modules, dist cũ)
COPY . .

# Lấy frontend đã build từ stage 1 → /app/dist (api.py tự serve tại "/")
COPY --from=frontend /app/dist ./dist

EXPOSE 8000

# .env KHÔNG nằm trong image — biến môi trường được nạp lúc run (docker compose env_file)
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
