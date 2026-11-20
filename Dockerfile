FROM ghcr.io/astral-sh/uv:0.9.5@sha256:f459f6f73a8c4ef5d69f4e6fbbdb8af751d6fa40ec34b39a1ab469acd6e289b7 AS uv
FROM node:22-bookworm-slim@sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da AS app
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /opt/fiscalm
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --python /usr/bin/python3
COPY tests/synthetic_world.py tests/pdf_fake.py tests/firms.py tests/ubl_fake.py ./tests/
COPY app/package*.json ./app/
RUN cd app && npm ci
COPY app ./app
COPY docs/openapi.json ./docs/openapi.json
ENV NEXT_TELEMETRY_DISABLED=1
RUN cd app && npm run build

FROM node:22-bookworm-slim@sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da AS ai-clis
WORKDIR /opt/fiscalm-ai
COPY deploy/ai-cli/package*.json ./
RUN npm ci --omit=dev --no-audit --no-fund

FROM python:3.11-slim-bookworm@sha256:94a8fee35252d06ab75856f15f12c7bae67b10e8a6da93c7fded82b2610926cd
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends bash coreutils util-linux procps poppler-utils && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 fiscalm \
    && mkdir /data /secrets && chown fiscalm:fiscalm /data /secrets && chmod 700 /data /secrets /home/fiscalm
WORKDIR /opt/fiscalm
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-cache
COPY --from=ai-clis /usr/local/bin/node /usr/local/bin/node
COPY --from=ai-clis /opt/fiscalm-ai /opt/fiscalm-ai
COPY --from=app /opt/fiscalm/app/out ./app/out
ENV DISABLE_AUTOUPDATER=1 FISCALM_HOME=/data \
    FISCALM_SECRETS=/secrets \
    HOME=/home/fiscalm FISCALM_APP_OUT=/opt/fiscalm/app/out PATH=/opt/fiscalm-ai/node_modules/.bin:/opt/fiscalm/.venv/bin:$PATH
USER fiscalm
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/healthz', timeout=3).read()"
ENTRYPOINT ["fiscalm"]
CMD ["serve"]
