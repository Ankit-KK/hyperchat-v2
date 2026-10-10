import ipaddress

from fastapi import APIRouter, Request

from app.auth.dependencies import client_ip
from app.core.config import get_settings

router = APIRouter(tags=["health"])


def _is_loopback(ip: str | None) -> bool:
    try:
        return ipaddress.ip_address(ip or "").is_loopback
    except ValueError:
        return False


@router.get("/health")
async def health(request: Request) -> dict:
    """Liveness: always 200 while the process is up; reports database reachability separately.

    Security audit F5: the public answer is only the status and the database reachability. Release, environment
    and database label are for the deploy checks on the server itself (curl 127.0.0.1:<port>/health): uvicorn
    trusts X-Forwarded-For only from Nginx on 127.0.0.1 and takes the client Nginx appended, so a proxied
    request always carries the real (non-loopback) client address and a forged header cannot change that."""
    settings = get_settings()
    db_status = await request.app.state.db.ping()
    if not _is_loopback(client_ip(request)):
        return {"status": "ok", "database": {"status": db_status}}
    return {
        "status": "ok",
        "service": "hyperchat-api",
        "release": settings.release,
        "environment": settings.app_env,
        "database": {"label": settings.db_label, "status": db_status},
    }
