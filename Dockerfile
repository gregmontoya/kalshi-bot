FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir httpx python-dotenv cryptography

COPY src/ ./src/
COPY scripts/ ./scripts/
COPY tests/ ./tests/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

VOLUME ["/app/data"]

CMD ["python", "scripts/run_recorder.py", "--db", "/app/data/recorder.sqlite", "--interval-min", "5"]
