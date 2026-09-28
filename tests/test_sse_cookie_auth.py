from types import SimpleNamespace
import time

import jwt
import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from control_plane.config import CP_JWT_ALG, CP_JWT_SECRET
from control_plane.database import get_db
from control_plane.routes import auth, stream
from control_plane.business import parsing


class _Query:
    def __init__(self, user):
        self.user = user

    def filter(self, *_conditions):
        return self

    def first(self):
        return self.user


class _Db:
    def __init__(self, user):
        self.user = user

    def query(self, _model):
        return _Query(self.user)


@pytest.fixture
def client(monkeypatch):
    user = SimpleNamespace(id=7, tenant_id=3, role="tenant_admin", is_active=True)
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(stream.router)
    app.include_router(parsing.router)
    db = _Db(user)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[stream.get_cp_db] = lambda: db
    # The SSE generator is deliberately not consumed: these tests cover the HTTP auth gate.
    monkeypatch.setattr(stream, "StreamingResponse", lambda *_args, **_kwargs: Response(status_code=200))
    monkeypatch.setattr(parsing, "StreamingResponse", lambda *_args, **_kwargs: Response(status_code=200))
    with TestClient(app, base_url="https://corebot.example") as test_client:
        yield test_client


def _bearer():
    return {"Authorization": f"Bearer {auth.create_access_token(7, 3, 'tenant_admin')}"}


def test_stream_session_requires_bearer(client):
    response = client.post("/auth/stream-session")
    assert response.status_code == 401
    assert "set-cookie" not in response.headers


def test_stream_cookie_flags_and_lifetime(client):
    response = client.post("/auth/stream-session", headers=_bearer())
    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    for flag in ("corebot_stream=", "HttpOnly", "SameSite=strict", "Secure", "Path=/business", "Max-Age=900"):
        assert flag in cookie
    payload = jwt.decode(client.cookies[auth.STREAM_COOKIE_NAME], CP_JWT_SECRET, algorithms=[CP_JWT_ALG])
    assert payload["type"] == "access"
    assert payload["sub"] == "7"
    assert 0 < payload["exp"] - time.time() <= 900
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/business/stream", "/business/parsing/stream"])
def test_sse_accepts_cookie_or_bearer_and_rejects_query(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers=_bearer()).status_code == 200
    assert client.post("/auth/stream-session", headers=_bearer()).status_code == 204
    assert client.get(path).status_code == 200
    assert client.get(path, params={"token": "anything"}).status_code == 400
    assert client.get(path, params={"token": "anything"}, headers=_bearer()).status_code == 400
    assert client.delete("/auth/stream-session").status_code == 204
    assert client.get(path).status_code == 401


def test_invalid_cookie_does_not_expose_jwt_exception(client):
    client.cookies.set(auth.STREAM_COOKIE_NAME, "not-a-jwt", path="/business")
    response = client.get("/business/parsing/stream")
    assert response.status_code == 401
    assert "not enough segments" not in response.text.lower()


def test_malformed_subject_is_rejected_by_both_streams(client):
    token = jwt.encode(
        {"sub": "not-an-integer", "type": "access", "exp": int(time.time()) + 60},
        CP_JWT_SECRET,
        algorithm=CP_JWT_ALG,
    )
    for path in ("/business/stream", "/business/parsing/stream"):
        response = client.get(path, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid token"}
