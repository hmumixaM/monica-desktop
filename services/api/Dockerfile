FROM python:3.12-slim AS dependencies
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /usr/local/bin/uv
COPY services/api/pyproject.toml services/api/uv.lock ./
RUN uv sync --locked --no-dev --compile-bytecode
ENV PYTHONPATH=/app UV_NO_SYNC=1 PYTHONDONTWRITEBYTECODE=1

FROM dependencies AS api
COPY services/api/app ./app
COPY migrations /migrations
COPY infra/wechat/prefetch-sns-images.py /app/wechat/prefetch-sns-images.py
RUN .venv/bin/python -m app.city_reference && .venv/bin/python -m compileall -q app
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM dependencies AS worker
COPY services/api/app ./app
COPY services/worker/worker.py ./worker.py
CMD ["/app/.venv/bin/python", "worker.py"]
