FROM python:3.11-slim

WORKDIR /app

# System deps for PyMuPDF and asyncpg
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY agents/doc_agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Agent source
COPY agents/doc_agent/src/ /app/src/

# Shared libraries (database, mq)
COPY shared/ /app/shared/

ENV PYTHONPATH=/app

EXPOSE 8002

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8002"]
