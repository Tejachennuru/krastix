import time
from celery.utils.log import get_task_logger
from shared.mq import celery_app

# Setup Celery Logger
logger = get_task_logger(__name__)

@celery_app.task(name="agents.perform_task")
def perform_task(instruction: str, user_id: str):
    """
    A dummy task to verify the queue pipeline on Oracle Cloud.
    """
    logger.info(f"🚀 RECEIVED TASK: {instruction} for User: {user_id}")
    
    # Simulate robust work (Wait 2 seconds)
    time.sleep(2)
    
    logger.info("✅ TASK COMPLETED SUCCESSFULLY")
    
    return {
        "status": "success", 
        "message": f"Processed '{instruction}' for {user_id}",
        "timestamp": time.time()
    }
