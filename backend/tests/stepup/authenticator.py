"""A software WebAuthn authenticator for tests: a real P-256 key that answers registration and assertion requests
the way a browser + platform authenticator would, with every field the verifier checks overridable, so each check
can be failed on purpose. Test-only; never imported by the application."""

import hashlib
import json
import os
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from app.stepup.canonical import b64url

UP, UV, AT = 0x01, 0x04, 0x40


class SoftAuthenticator:
    def __init__(self, rp_id: str, origin: str) -> None:
        self.rp_id, self.origin = rp_id, origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(32)
        self.sign_count = 0
        self.user_handle: bytes | None = None

    def cose_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})

    def _client_data(self, kind: str, challenge: str, origin: str | None, extra: dict | None) -> bytes:
        data = {"type": kind, "challenge": challenge, "origin": origin or self.origin, "crossOrigin": False}
        data.update(extra or {})
        return json.dumps(data).encode()

    def register(self, options: dict, *, origin: str | None = None, rp_id: str | None = None, uv: bool = True,
                 challenge: str | None = None, client_extra: dict | None = None) -> dict:
        self.user_handle = options["user"]["id"]
        client_data = self._client_data("webauthn.create", challenge or options["challenge"], origin, client_extra)
        auth_data = (hashlib.sha256((rp_id or self.rp_id).encode()).digest() + bytes([UP | (UV if uv else 0) | AT])
                     + struct.pack(">I", self.sign_count) + bytes(16)
                     + struct.pack(">H", len(self.credential_id)) + self.credential_id + self.cose_key())
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        cid = b64url(self.credential_id)
        return {"id": cid, "rawId": cid, "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": b64url(client_data), "attestationObject": b64url(attestation),
                             "transports": ["internal"]}}

    def assert_(self, options: dict, *, origin: str | None = None, rp_id: str | None = None, uv: bool = True,
                challenge: str | None = None, sign_count: int | None = None, client_extra: dict | None = None,
                key: ec.EllipticCurvePrivateKey | None = None) -> dict:
        self.sign_count = self.sign_count + 1 if sign_count is None else sign_count
        client_data = self._client_data("webauthn.get", challenge or options["challenge"], origin, client_extra)
        auth_data = (hashlib.sha256((rp_id or self.rp_id).encode()).digest() + bytes([UP | (UV if uv else 0)])
                     + struct.pack(">I", self.sign_count))
        signature = (key or self.key).sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        cid = b64url(self.credential_id)
        return {"id": cid, "rawId": cid, "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": b64url(client_data), "authenticatorData": b64url(auth_data),
                             "signature": b64url(signature), "userHandle": self.user_handle}}
