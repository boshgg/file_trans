FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Etc/UTC

WORKDIR /app

RUN groupadd --gid 10001 filehub \
    && useradd --uid 10001 --gid filehub --no-create-home filehub \
    && mkdir -p /data /app/.filehub \
    && chown -R filehub:filehub /data /app/.filehub

COPY --chown=filehub:filehub lan_file_hub.py ./
COPY --chown=filehub:filehub web ./web

USER filehub
EXPOSE 8765
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/status', timeout=3).close()"

ENTRYPOINT ["python", "-B", "lan_file_hub.py"]
CMD ["--host", "0.0.0.0", "--port", "8765", "--data-dir", "/data", "--no-auto-cleanup"]
