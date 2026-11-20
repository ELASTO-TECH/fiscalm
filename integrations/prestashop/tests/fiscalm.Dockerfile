FROM ghcr.io/astral-sh/uv:0.9.5 AS uv
FROM python:3.11-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /opt/fiscalm
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-cache
RUN useradd --create-home --uid 10001 fiscalm
ENV PATH=/opt/fiscalm/.venv/bin:$PATH
USER fiscalm
