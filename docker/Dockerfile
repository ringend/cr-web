FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY backend/ .

# Copy frontend code
COPY frontend/ ./frontend/

# Copy config directory
COPY config/ ./config/

# Match the host nas-sync group so bind-mounted files stay group-accessible.
RUN groupadd --gid 2001 nas-sync \
    && useradd --uid 1000 --gid 2001 --create-home appuser \
    && chown -R appuser:nas-sync /app \
    && chmod -R g=u /app

# Serve static files from the frontend directory
# (This is handled by FastAPI mounting it, but the files must be present)

EXPOSE 8080

USER appuser:nas-sync

CMD ["sh", "-lc", "umask 0002 && exec uvicorn main:app --host 0.0.0.0 --port 8080"]
