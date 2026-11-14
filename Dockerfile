FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY chase ./chase
COPY synthetic ./synthetic
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install --no-cache-dir ".[postgres]" && useradd --system --uid 10001 chase
USER chase
EXPOSE 8000
# One worker: the engine also takes a database lock, so more workers are safe but not faster.
CMD ["sh", "-c", "alembic upgrade head && uvicorn chase.api.main:app --host 0.0.0.0 --port 8000"]
