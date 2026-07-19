import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit


BASE_URL = os.environ["LINK_METRICS_CONFORMANCE_URL"].rstrip("/")
PUBLIC_BENCHMARK_JWT_KEY = (
    Path(__file__).resolve().parents[1] / "fixtures" / "jwt-hs256.key"
).read_bytes().strip()


def request_api(
    method: str,
    path: str,
    *,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, str, str | None]:
    parsed_url = urlsplit(BASE_URL)
    connection = http.client.HTTPConnection(parsed_url.hostname, parsed_url.port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return (
            response.status,
            response.read(),
            response.headers.get_content_type(),
            response.headers.get("Location"),
        )
    finally:
        connection.close()


def json_request(
    method: str,
    path: str,
    body: dict,
    *,
    token: str | None = None,
) -> tuple[int, bytes, str, str | None]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return request_api(
        method,
        path,
        body=json.dumps(body, separators=(",", ":")).encode(),
        headers=headers,
    )


def register_and_login(email: str) -> tuple[dict, str]:
    registration = json_request(
        "POST",
        "/api/auth/register",
        {"email": email, "password": "benchmark-password"},
    )
    assert registration[0] == 201
    login = json_request(
        "POST",
        "/api/auth/login",
        {"email": email, "password": "benchmark-password"},
    )
    assert login[0] == 200
    return json.loads(registration[1]), json.loads(login[1])["token"]


def decode_jwt_part(part: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(part + ("=" * (-len(part) % 4))))


def test_readiness_is_exact() -> None:
    assert request_api("GET", "/health") == (204, b"", "text/plain", None)


def test_registration_and_login_follow_the_standard_identity_profile() -> None:
    issued_after = int(time.time())
    registration = json_request(
        "POST",
        "/api/auth/register",
        {"email": "Conformance.User@Example.com", "password": "benchmark-password"},
    )

    assert registration[0] == 201
    assert registration[2] == "application/json"
    user = json.loads(registration[1])
    assert set(user) == {"id", "email", "createdAt"}
    assert uuid.UUID(user["id"]).version == 7
    assert user["email"] == "conformance.user@example.com"
    assert re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z",
        user["createdAt"],
    )

    login = json_request(
        "POST",
        "/api/auth/login",
        {"email": "conformance.user@example.com", "password": "benchmark-password"},
    )
    assert login[0] == 200
    token = json.loads(login[1])["token"]
    encoded_header, encoded_claims, encoded_signature = token.split(".")
    header = decode_jwt_part(encoded_header)
    claims = decode_jwt_part(encoded_claims)
    expected_signature = hmac.new(
        PUBLIC_BENCHMARK_JWT_KEY,
        f"{encoded_header}.{encoded_claims}".encode(),
        hashlib.sha256,
    ).digest()
    assert header == {"alg": "HS256", "typ": "JWT"}
    assert claims["sub"] == user["id"]
    assert claims["iss"] == "link-metrics"
    assert claims["aud"] == "link-metrics-api"
    assert issued_after <= claims["iat"] <= int(time.time())
    assert claims["exp"] == claims["iat"] + 900
    assert hmac.compare_digest(
        base64.urlsafe_b64decode(encoded_signature + ("=" * (-len(encoded_signature) % 4))),
        expected_signature,
    )


def test_short_link_workflow_preserves_exact_state_transitions() -> None:
    user, token = register_and_login("workflow@example.com")
    destination = "https://Example.COM/a%2Fb?source=Conformance#Result"
    creation = json_request("POST", "/api/links", {"url": destination}, token=token)
    assert creation[0] == 201
    short_link = json.loads(creation[1])
    assert short_link["userId"] == user["id"]
    assert re.fullmatch(r"[0-9A-Za-z]{8}", short_link["shortCode"])
    assert short_link["originalUrl"] == destination
    short_code = short_link["shortCode"]

    never_clicked = request_api(
        "GET",
        f"/api/links/{short_code}/stats",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert never_clicked[0] == 200
    assert json.loads(never_clicked[1]) == {
        "shortCode": short_code,
        "originalUrl": destination,
        "clickCount": 0,
        "lastClickedAt": None,
    }

    resolution = request_api("GET", f"/{short_code}")
    assert resolution == (302, b"", "text/plain", destination)
    clicked = request_api(
        "GET",
        f"/api/links/{short_code}/stats",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert clicked[0] == 200
    clicked_statistics = json.loads(clicked[1])
    assert clicked_statistics["clickCount"] == 1
    assert re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z",
        clicked_statistics["lastClickedAt"],
    )


def test_authentication_ownership_and_canonical_errors_are_enforced() -> None:
    _, owner_token = register_and_login("ownership-owner@example.com")
    _, other_token = register_and_login("ownership-other@example.com")
    creation = json_request(
        "POST",
        "/api/links",
        {"url": "https://example.com/private"},
        token=owner_token,
    )
    assert creation[0] == 201
    short_code = json.loads(creation[1])["shortCode"]

    non_owned = request_api(
        "GET",
        f"/api/links/{short_code}/stats",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    missing = request_api(
        "GET",
        "/api/links/zzzzzzzz/stats",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    unauthenticated = request_api("GET", f"/api/links/{short_code}/stats")
    wrong_password = json_request(
        "POST",
        "/api/auth/login",
        {"email": "ownership-owner@example.com", "password": "wrong-password"},
    )
    duplicate = json_request(
        "POST",
        "/api/auth/register",
        {"email": "OWNERSHIP-OWNER@EXAMPLE.COM", "password": "benchmark-password"},
    )

    assert non_owned == (404, b'{"error":"not_found"}', "application/json", None)
    assert missing == non_owned
    assert unauthenticated == (401, b'{"error":"unauthorized"}', "application/json", None)
    assert wrong_password == (401, b'{"error":"unauthorized"}', "application/json", None)
    assert duplicate == (409, b'{"error":"conflict"}', "application/json", None)
