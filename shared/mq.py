import os
import time
from celery import Celery

# 1. Get Redis URL from Environment
# This allows switching between 'localhost' (dev) and 'redis' (docker service name)
redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# 2. Configure Celery
# We remove specific includes here to keep the mq module generic.
# Workers must load their own modules via the --include flag or imports.
celery_app = Celery(
    "krastix_mq",
    broker=redis_url,
    backend=redis_url,
    # include=["agents.tasks"]  <-- REMOVED to support microservices
)

# 3. Tuning for Production / Oracle Cloud
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    worker_concurrency=4,  # Adjust based on ARM core count (Ampere usually has 4 OCPU)
)

print(f"✅ Celery Initialized with Broker: {redis_url}")
