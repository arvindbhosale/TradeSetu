# TradeSetu, hosted. Built by Dokploy (or `docker build .`) from the repo root.
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /build
COPY . .
RUN pip install --no-cache-dir ".[desk]"

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRADINGAGENTS_IN_DOCKER=1 \
    TRADINGAGENTS_NO_UPDATE_CHECK=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000 \
    TRADESETU_HOME=/data/desk \
    TRADINGAGENTS_RESULTS_DIR=/data/results \
    TRADINGAGENTS_CACHE_DIR=/data/cache \
    TRADINGAGENTS_MEMORY_LOG_PATH=/data/memory/trading_memory.md

COPY --from=builder /opt/venv /opt/venv

# Everything the desk keeps (settings, keys, paper book, algos, order journal,
# price cache) lives in /data. Mount a volume there or it is lost on redeploy.
RUN useradd --create-home --uid 1000 desk && install -d -o desk -g desk /data
USER desk
WORKDIR /home/desk
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz', timeout=4)"

CMD ["tradesetu", "--serve"]
