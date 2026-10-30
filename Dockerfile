FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV APP_BASE_DIR=/app
ENV PORT=8000

# Install Vietnamese-capable TrueType fonts (DejaVu Sans) & TLS certificates for Linux Cloud
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (cached Docker layer)
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# Copy full-stack Unified Cloud Server assets (Backend + React Admin SPA + TikTok Studio + News Portal)
COPY backend/ /app/backend/
COPY frontend/dist/ /app/frontend/dist/
COPY aipulse_deploy/ /app/aipulse_deploy/
COPY scripts/ /app/scripts/
COPY workspaces/tiktok-fashion-ai-automation/ /app/workspaces/tiktok-fashion-ai-automation/

# Prepare writable runtime directories (compatible with both Render & Hugging Face Spaces non-root uid 1000)
RUN mkdir -p \
    /app/data \
    /app/tools \
    /app/reports \
    /app/workspaces/tiktok-fashion-ai-automation/config \
    /app/workspaces/tiktok-fashion-ai-automation/uploads/model_images \
    /app/workspaces/tiktok-fashion-ai-automation/uploads/garments \
    /app/workspaces/tiktok-fashion-ai-automation/drive_sync/01_Anh_Nguoi_Mau \
    /app/workspaces/tiktok-fashion-ai-automation/cache/thumbnails \
    /app/workspaces/tiktok-fashion-ai-automation/cache/vton \
    /app/workspaces/tiktok-fashion-ai-automation/published/tryon_previews \
    /app/workspaces/tiktok-fashion-ai-automation/published/scene_keyframes \
    /app/workspaces/tiktok-fashion-ai-automation/published/final_videos \
    && chmod -R 777 /app/data /app/tools /app/reports /app/aipulse_deploy /app/workspaces /app/backend

WORKDIR /app/backend
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
