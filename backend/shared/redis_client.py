import os
import redis
from celery import Celery

# Redis connection
redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_client = redis.from_url(redis_url, decode_responses=True)

# Celery app for task queue
celery_app = Celery(
    "orchestrator",
    broker=redis_url,
    backend=redis_url
)

celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    task_routes={
        'agents.research_worker.execute_task': {'queue': 'research_queue'},
        'agents.crm_worker.execute_task': {'queue': 'crm_queue'},
        'agents.form_worker.execute_task': {'queue': 'form_queue'},
    },
    include=[
        'agents.research_worker.worker',
        'agents.crm_worker.worker',
        'agents.form_worker.worker'
    ]
)