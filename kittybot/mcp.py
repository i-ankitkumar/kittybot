"""Minimal MCP client with OAuth 2.1 + PKCE.

Both Swiggy and Zepto publish official MCP servers with OAuth. The flow is
the same for both:

1. **Dynamic Client Registration** (RFC 7591) - no app to create by hand.
2. **Authorization Code + PKCE** in the user's browser.
3. **Token exchange**, then call tools over JSON-RPC.

Only ``tools:read``-style scopes are ever requested: this tool reads order
history and nothing else.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

DEFAULT_CALLBACK_PORT = 8765
DEFAULT_CALLBACK_PATH = "/callback"
REDIRECT_URI = f"http://localhost:{DEFAULT_CALLBACK_PORT}{DEFAULT_CALLBACK_PATH}"


class McpError(RuntimeError):
    """Raised when an MCP server rejects a call."""


def pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def register_client(
    registration_endpoint: str,
    *,
    client_name: str = "kittybot",
    redirect_uri: str = REDIRECT_URI,
    scope: str = "mcp:tools",
) -> dict:
    """Register a client dynamically. Returns the registration payload."""
    payload = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": scope,
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.post(registration_endpoint, json=payload)
    if response.status_code not in (200, 201):
        raise McpError(
            f"client registration failed ({response.status_code}): {response.text[:300]}"
        )
    return response.json()


def build_authorize_url(
    authorization_endpoint: str,
    *,
    client_id: str,
    code_challenge: str,
    state: str,
    redirect_uri: str = REDIRECT_URI,
    scope: str = "mcp:tools",
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            "scope": scope,
        }
    )
    return f"{authorization_endpoint}?{query}"


def exchange_code(
    token_endpoint: str,
    *,
    client_id: str,
    code: str,
    code_verifier: str,
    redirect_uri: str = REDIRECT_URI,
) -> dict:
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
    }
    if client_id:
        payload["client_id"] = client_id
    with httpx.Client(timeout=30.0) as client:
        response = client.post(token_endpoint, json=payload)
    if response.status_code != 200:
        raise McpError(f"token exchange failed ({response.status_code}): {response.text[:300]}")
    data = response.json()
    data["expires_at"] = time.time() + float(data.get("expires_in", 432000))
    data["client_id"] = client_id
    return data


def refresh_token(
    token_endpoint: str,
    *,
    client_id: str,
    refresh: str,
    scope: str = "mcp:tools",
) -> dict:
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": client_id,
        "scope": scope,
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.post(token_endpoint, json=payload)
    if response.status_code != 200:
        raise McpError(f"token refresh failed ({response.status_code}): {response.text[:300]}")
    data = response.json()
    data["expires_at"] = time.time() + float(data.get("expires_in", 432000))
    data["client_id"] = client_id
    data.setdefault("refresh_token", refresh)
    return data


def save_token(path: Path, token: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(token, indent=2))
    path.chmod(0o600)


def load_token(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


class _CallbackHandler(BaseHTTPRequestHandler):
    """Captures the OAuth authorization code."""

    code: str | None = None
    state: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # noqa: N802
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(self.path).query)
        if "code" in query:
            type(self).code = query["code"][0]
            type(self).state = query.get("state", [None])[0]
            body = b"<html><body style='font-family:sans-serif;padding:2rem'>"
            body += b"<h2>kittybot is connected.</h2>"
            body += b"<p>You can close this tab and go back to your terminal.</p>"
            body += b"</body></html>"
            self.send_response(200)
        else:
            type(self).error = query.get("error", ["unknown error"])[0]
            body = b"<html><body><h2>Connection failed</h2><p>See your terminal.</p></body></html>"
            self.send_response(400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # keep the console quiet
        return


def run_local_callback(
    authorize_url: str,
    *,
    expected_state: str,
    port: int = DEFAULT_CALLBACK_PORT,
    timeout: int = 300,
) -> str:
    """Serve the callback page and wait for the authorization code.

    Prints the URL to open; the user authorises in their browser and we
    capture the code.
    """
    _CallbackHandler.code = None
    _CallbackHandler.state = None
    _CallbackHandler.error = None

    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = timeout

    import threading

    print("\nOpen this URL in your browser to authorise:\n")
    print(f"  {authorize_url}\n")
    print(f"Waiting for the callback on {REDIRECT_URI} (Ctrl-C to cancel)...\n")

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _CallbackHandler.code or _CallbackHandler.error:
                break
            time.sleep(0.3)
    finally:
        server.shutdown()
        server.server_close()

    if _CallbackHandler.error:
        raise McpError(f"authorisation failed: {_CallbackHandler.error}")
    if not _CallbackHandler.code:
        raise McpError("timed out waiting for the OAuth callback")
    if _CallbackHandler.state and _CallbackHandler.state != expected_state:
        raise McpError("OAuth state mismatch; aborting")
    return _CallbackHandler.code


class McpClient:
    """A tiny JSON-RPC client for a Streamable-HTTP MCP server."""

    def __init__(self, endpoint: str, *, http: httpx.Client | None = None) -> None:
        self.endpoint = endpoint
        self._http = http or httpx.Client(timeout=45.0)
        self._owns = http is None
        self._request_id = 0

    def close(self) -> None:
        if self._owns:
            self._http.close()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def call_tool(self, access_token: str, name: str, arguments: dict[str, Any]) -> dict:
        """Invoke an MCP tool and return its unwrapped payload."""
        response = self._http.post(
            self.endpoint,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
                "id": self._next_id(),
            },
        )
        if response.status_code == 401:
            raise McpError("the server rejected our token; reconnect")
        if response.status_code != 200:
            raise McpError(f"MCP call failed ({response.status_code}): {response.text[:300]}")
        return unwrap_mcp_payload(response)

    def list_tools(self, access_token: str) -> list[dict]:
        response = self._http.post(
            self.endpoint,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "method": "tools/list", "id": self._next_id()},
        )
        if response.status_code != 200:
            raise McpError(f"tools/list failed ({response.status_code})")
        payload = _parse_body(response)
        return payload.get("result", {}).get("tools", [])


def _parse_body(response: httpx.Response) -> dict:
    """Handle both plain JSON and SSE-framed MCP responses."""
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in response.text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
        raise McpError("could not parse the SSE response")
    try:
        return response.json()
    except Exception as exc:
        raise McpError(f"could not parse the MCP response: {exc}") from exc


def unwrap_mcp_payload(response: httpx.Response) -> dict:
    """Pull the JSON data out of an MCP tool result."""
    payload = _parse_body(response)
    if payload.get("error"):
        raise McpError(f"MCP error: {payload['error']}")
    result = payload.get("result", payload)
    content = result.get("content") if isinstance(result, dict) else None
    if content:
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                try:
                    inner = json.loads(block["text"])
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
                if isinstance(inner, dict):
                    if inner.get("success") is False:
                        raise McpError(str(inner.get("error", "the tool call failed")))
                    return inner.get("data", inner)
    if isinstance(result, dict):
        if result.get("success") is False:
            raise McpError(str(result.get("error", "the tool call failed")))
        return result.get("data", result)
    return {}
