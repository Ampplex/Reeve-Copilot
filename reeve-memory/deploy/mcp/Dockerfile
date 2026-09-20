FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BACKUP_DIR=/app/data/backups \
    TEMP_BUFFER_FAILED_LOG_PATH=/app/data/tmp_failed.json

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY threelane_memory /app/threelane_memory
COPY manage_keys.py /app/manage_keys.py

RUN pip install --no-cache-dir ".[mcp]"

# Run as an unprivileged user. Application code stays root-owned (not writable
# by the runtime user); only /app/data — where backups and the write-buffer
# failed-log live — is owned by the app user.
RUN groupadd --system --gid 10001 appuser \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home appuser \
    && mkdir -p /app/data/backups \
    && chown -R appuser:appuser /app/data

USER appuser

EXPOSE 8000

CMD ["reeve-mcp", "--transport", "sse", "--host", "0.0.0.0", "--port", "8000"]
