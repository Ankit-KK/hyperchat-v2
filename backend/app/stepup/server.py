"""The step-up verifier service: an internal HTTP API on a Unix socket, run by systemd under its own OS user
(deploy/systemd/hyperchat-stepup.{socket,service}):

    uvicorn app.stepup.server:create_app --factory --fd 3

The socket is created by systemd (hyperchat-stepup.socket) as <verifier user>:<API group> 0660, so only the API's
OS user (and root) can call it; the service has no network access at all. The API forwards the administrator id,
the session id and the browser's WebAuthn response; see app.stepup.verifier for what is checked.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from app.core.logging import configure_logging
from app.stepup.config import StepupSettings, get_stepup_settings, unexpected_keys
from app.stepup.verifier import StepupError, Verifier

ID = Field(min_length=36, max_length=36)


class Who(BaseModel):
    user_id: str = ID
    session_id: str = ID


class AssertionOptionsIn(Who):
    action: str = Field(max_length=64)
    target: str = Field(max_length=200)
    params: dict = Field(default_factory=dict)


class VerifyIn(Who):
    challenge_id: str = ID
    credential: dict


def create_app(settings: StepupSettings | None = None, environ: dict | None = None) -> FastAPI:
    environ = os.environ if environ is None else environ
    stray = unexpected_keys(environ)
    if stray:  # a copied api.env (or any application secret) in the verifier's environment defeats the boundary
        raise RuntimeError("the step-up verifier refuses to start with these variables in its environment: "
                           + ", ".join(stray))
    settings = settings or get_stepup_settings()
    configure_logging(settings.log_level)
    pool = AsyncConnectionPool(conninfo=settings.conninfo(), min_size=1, max_size=4, open=False,
                               kwargs={"autocommit": True}, name="hyperchat-stepup")

    @asynccontextmanager
    async def connect():
        async with pool.connection(timeout=5.0) as conn:
            yield conn

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await pool.open(wait=False)
        try:
            yield
        finally:
            await pool.close()

    app = FastAPI(title="hyperchat-stepup", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.verifier = verifier = Verifier(settings, connect)

    @app.exception_handler(StepupError)
    async def _refused(_request: Request, exc: StepupError) -> JSONResponse:
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    @app.get("/health")
    async def health() -> dict:
        async with connect() as conn, conn.cursor() as cur:
            await cur.execute("SELECT state FROM public.stepup_enforcement")
            state = (await cur.fetchone())[0]
        return {"status": "ok", "enforcement": state, "db_label": settings.db_label}

    @app.post("/v1/credentials")
    async def list_credentials(body: Who) -> dict:
        return await verifier.list_credentials(body.user_id, body.session_id)

    @app.post("/v1/registration/options")
    async def registration_options(body: Who) -> dict:
        return await verifier.registration_options(body.user_id, body.session_id)

    @app.post("/v1/registration/verify")
    async def registration_verify(body: VerifyIn) -> dict:
        return await verifier.registration_verify(body.user_id, body.session_id, body.challenge_id, body.credential)

    @app.post("/v1/assertion/options")
    async def assertion_options(body: AssertionOptionsIn) -> dict:
        return await verifier.assertion_options(body.user_id, body.session_id, body.action, body.target, body.params)

    @app.post("/v1/assertion/verify")
    async def assertion_verify(body: VerifyIn) -> dict:
        return await verifier.assertion_verify(body.user_id, body.session_id, body.challenge_id, body.credential)

    return app
