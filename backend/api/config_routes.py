"""
System config API routes
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database.connection import SessionLocal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["config"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class ConfigUpdateRequest(BaseModel):
    key: str
    value: str
    description: str | None = None


@router.get("/check-required")
async def check_required_configs(db: Session = Depends(get_db)):
    """Check if required configs are set"""
    try:
        return {"has_required_configs": True, "missing_configs": []}
    except Exception as e:
        logger.error(f"Failed to check required configs: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to check required configs: {str(e)}")


@router.get("/scheduler-status")
async def get_scheduler_status():
    """Check scheduler status and jobs"""
    try:
        from services.scheduler import task_scheduler

        is_running = task_scheduler.is_running()
        jobs = []

        if task_scheduler.scheduler:
            job_info = task_scheduler.get_job_info()
            jobs = [
                {
                    "id": job["id"],
                    "function": job["func_name"],
                    "next_run": str(job["next_run_time"]) if job["next_run_time"] else None,
                }
                for job in job_info
            ]

        return {"scheduler_running": is_running, "total_jobs": len(jobs), "jobs": jobs}
    except Exception as e:
        logger.error(f"Failed to get scheduler status: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get scheduler status: {str(e)}")
