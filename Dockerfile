# Template: build and smoke-test it yourself before relying on it (deploy/DEPLOY.md describes the supported, tested path: a VPS with systemd + Caddy).
FROM python:3.12-slim
RUN useradd --system --uid 10001 --no-create-home apex && mkdir -p /app /data && chown apex:apex /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apex ./apex
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER apex
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=4s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)"
# /data/web.json: host 0.0.0.0, trusted_proxy true, trusted_proxies = your docker network CIDR. Secrets via env only.
CMD ["python", "-m", "apex.cli", "--config", "/data/web.json", "web-serve"]
