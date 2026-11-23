FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="network-monitor" \
      org.opencontainers.image.description="Network quality and outage monitor with ICMP/TCP/UDP probes and MTR fault localization" \
      org.opencontainers.image.source="https://github.com/TaylorGotIT/network-monitor" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

# 监控依赖：ping / traceroute / mtr 必须在镜像里预装好
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        iputils-ping \
        traceroute \
        mtr-tiny \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY netmon ./netmon
COPY static ./static

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8888

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8888/login', timeout=3).getcode() == 200 else 1)"

CMD ["python", "app.py", \
     "--host", "0.0.0.0", \
     "--port", "8888", \
     "--config", "/data/config.json", \
     "--db", "/data/monitor.db", \
     "--static", "/app/static"]
