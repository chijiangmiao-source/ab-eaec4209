FROM python:3.11-slim

# 验收服务中的前端守卫测试与 JS 语法检查需要 node
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app/ ./app/
COPY tests/ ./tests/
COPY scripts/ ./scripts/
RUN chmod +x scripts/verify && mkdir -p /data

ENV NAV_HOST=0.0.0.0 \
    NAV_PORT=8080 \
    NAV_STORE=/data/state.json \
    PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=5s --timeout=3s --retries=10 --start-period=3s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

CMD ["python3", "-m", "app.server"]
