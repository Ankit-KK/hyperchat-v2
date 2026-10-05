from fastapi import APIRouter, Request

from app.core.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict:
    """Liveness: always 200 while the process is up; reports database reachability separately."""
    settings = get_settings()
    db_status = await request.app.state.db.ping()
    return {
        "status": "ok",
        "service": "hyperchat-api",
        "release": settings.release,
        "environment": settings.app_env,
        "database": {"label": settings.db_label, "status": db_status},
    }
