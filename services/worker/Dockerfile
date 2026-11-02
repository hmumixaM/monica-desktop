FROM python:3.12-slim
WORKDIR /app
COPY services/api/pyproject.toml services/api/uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --locked --no-dev
COPY services/worker/worker.py ./worker.py
COPY services/api/app ./app
ENV PYTHONPATH=/app UV_NO_SYNC=1 PYTHONDONTWRITEBYTECODE=1
CMD ["uv", "run", "python", "worker.py"]
