FROM python:3.11-slim
WORKDIR /app

# Install system dependencies (e.g. for psycopg2/asyncpg build if needed)
RUN apt-get update && apt-get install -y gcc libpq-dev && rm -rf /var/lib/apt/lists/*

COPY orchestrator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code (assuming context is project root)
COPY orchestrator/ /app/orchestrator
COPY shared/ /app/shared

# Set python path to allow imports like 'from shared...' and 'from orchestrator.src...'
ENV PYTHONPATH=/app

CMD ["uvicorn", "orchestrator.src.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
