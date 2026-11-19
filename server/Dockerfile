# Card discovery mini server: API + 2 workers + scheduler in one container (python -m cdserver dev).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY carddiscovery ./carddiscovery
COPY cdserver ./cdserver
RUN pip install -e ".[server]"

# Runs as a normal user, not root
RUN useradd --create-home app && mkdir -p /data && chown app /data
USER app

EXPOSE 8790
CMD ["python", "-m", "cdserver", "dev"]
