"""The API's side of the step-up verifier: a thin client over the verifier's Unix socket (S2 wires it to routes).

Nothing returned here is trusted by the database on the API's word: a grant or a session's assurance exists only
if the verifier wrote it (migration 0025), and the API can only consume a grant through public.stepup_consume().
"""

import httpx


class StepupUnavailable(Exception):
    pass


class StepupRefused(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status, self.code = status, code


class StepupClient:
    def __init__(self, socket_path: str | None = None, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 10.0) -> None:
        transport = transport or httpx.AsyncHTTPTransport(uds=socket_path)
        self._client = httpx.AsyncClient(transport=transport, base_url="http://stepup", timeout=timeout)

    async def _post(self, path: str, body: dict) -> dict:
        try:
            response = await self._client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise StepupUnavailable(type(exc).__name__) from None
        data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        if response.status_code != 200:
            raise StepupRefused(response.status_code, data.get("error", "refused") if isinstance(data, dict)
                                else "refused")
        return data

    async def registration_options(self, user_id: str, session_id: str) -> dict:
        return await self._post("/v1/registration/options", {"user_id": user_id, "session_id": session_id})

    async def registration_verify(self, user_id: str, session_id: str, challenge_id: str, credential: dict) -> dict:
        return await self._post("/v1/registration/verify", {"user_id": user_id, "session_id": session_id,
                                                            "challenge_id": challenge_id, "credential": credential})

    async def assertion_options(self, user_id: str, session_id: str, action: str, target: str,
                                params: dict | None = None) -> dict:
        return await self._post("/v1/assertion/options", {"user_id": user_id, "session_id": session_id,
                                                          "action": action, "target": target, "params": params or {}})

    async def assertion_verify(self, user_id: str, session_id: str, challenge_id: str, credential: dict) -> dict:
        return await self._post("/v1/assertion/verify", {"user_id": user_id, "session_id": session_id,
                                                         "challenge_id": challenge_id, "credential": credential})

    async def aclose(self) -> None:
        await self._client.aclose()
