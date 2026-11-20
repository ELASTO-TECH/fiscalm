FROM ghcr.io/astral-sh/uv:0.9.5 AS uv
FROM python:3.11-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /engine
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev
ENV PATH=/engine/.venv/bin:$PATH
ENTRYPOINT ["python", "/suite/real_api.py"]
