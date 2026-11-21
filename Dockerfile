# syntax=docker/dockerfile:1
# Backend: the card-discovery server (API + 2 workers + scheduler: `python -m cdserver dev`).
# Includes the tools statement PDFs need (pdftoppm, tesseract) and, for the shadow Haiku fallback,
# Node and the Claude Code CLI (`claude -p`, logged in with CLAUDE_CODE_OAUTH_TOKEN at run time).
# Node 22 (the Claude CLI needs >= 22; Debian's package is 20), taken from the official image
FROM node:22-slim AS node

FROM python:3.12-slim

ARG WITH_CLAUDE_CLI=1
# Slow or flaky networks: long timeouts and retries for pip and npm
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=10
WORKDIR /app

# OCR for statement PDFs whose card name is an image (ICICI, RBL, YES)
RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr \
 && rm -rf /var/lib/apt/lists/*

# Claude Code CLI for the shadow Haiku fallback
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && if [ "$WITH_CLAUDE_CLI" = "1" ]; then \
      npm install -g --fetch-retries=5 --fetch-timeout=120000 @anthropic-ai/claude-code && npm cache clean --force; fi

COPY pyproject.toml README.md ./
COPY carddiscovery ./carddiscovery
COPY cdserver ./cdserver
COPY skills ./skills
# the download cache survives a failed build, so a retry doesn't fetch everything again
RUN --mount=type=cache,target=/root/.cache/pip pip install -e ".[server,llm]"

# Runs as a normal user, not root; /data holds the vault master key and server data (a volume)
RUN useradd --create-home app && mkdir -p /data && chown app /data
USER app

EXPOSE 8790
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8790), 2)" || exit 1
CMD ["python", "-m", "cdserver", "dev"]
