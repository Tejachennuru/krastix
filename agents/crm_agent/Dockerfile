FROM python:3.11-slim
WORKDIR /app

COPY agents/crm_agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy Agent Code and Shared Module
COPY agents/crm_agent/ /app/agent
COPY shared/ /app/shared

ENV PYTHONPATH=/app

CMD ["celery", "-A", "agent.src.worker:celery_app", "worker", "--loglevel=info"]
