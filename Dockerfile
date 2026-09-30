FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN useradd --create-home --uid 10001 smi && mkdir -p /data /backups /config && chown smi:smi /data /backups
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . "psycopg[binary]>=3.1"

USER smi
ENV SMI_DATA_DIR=/data SMI_BACKUP_DIR=/backups SMI_CONFIG_DIR=/config SMI_DATABASE_URL=sqlite:////data/smi_agent.db
VOLUME ["/data", "/backups"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health/live', timeout=4).status == 200 else 1)"
ENTRYPOINT ["smi-agent"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
