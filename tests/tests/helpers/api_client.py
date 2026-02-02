"""HTTP client wrapper with request/response recording for test reporting."""

from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx


@dataclass
class RequestResponsePair:
    """Recorded HTTP exchange."""

    request_method: str
    request_url: str
    request_headers: dict[str, str]
    request_body: str | None
    response_status: int
    response_headers: dict[str, str]
    response_body: str | None
    timestamp: str

    def to_dict(self) -> dict:
        return {
            "request": {
                "method": self.request_method,
                "url": self.request_url,
                "headers": self.request_headers,
                "body": self.request_body,
            },
            "response": {
                "status": self.response_status,
                "headers": self.response_headers,
                "body": self.response_body,
            },
            "timestamp": self.timestamp,
        }


_MASKED_PREFIXES = {
    "authorization": "Bearer ccas_***",
    "x-anthropic-key": "sk-ant-***",
}

_MAX_RESPONSE_BODY_LEN = 2000


def _mask_headers(headers: dict[str, str]) -> dict[str, str]:
    """Mask sensitive header values."""
    masked = {}
    for k, v in headers.items():
        lower = k.lower()
        if lower in _MASKED_PREFIXES:
            masked[k] = _MASKED_PREFIXES[lower]
        else:
            masked[k] = v
    return masked


def _serialize_body(body: bytes | str | None, content_type: str = "") -> str | None:
    if body is None:
        return None
    if isinstance(body, bytes):
        if "json" in content_type or "text" in content_type:
            try:
                decoded = body.decode("utf-8")
                if len(decoded) > _MAX_RESPONSE_BODY_LEN:
                    return decoded[:_MAX_RESPONSE_BODY_LEN] + "... (truncated)"
                return decoded
            except UnicodeDecodeError:
                pass
        return f"[binary {len(body)} bytes]"
    if len(body) > _MAX_RESPONSE_BODY_LEN:
        return body[:_MAX_RESPONSE_BODY_LEN] + "... (truncated)"
    return body


class ApiClient:
    """HTTP client wrapper with request/response recording."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.history: list[RequestResponsePair] = []
        self._client = httpx.Client(base_url=self.base_url, timeout=30.0)

    def _record(self, request: httpx.Request, response: httpx.Response) -> None:
        req_headers = _mask_headers(dict(request.headers))
        req_content_type = request.headers.get("content-type", "")
        resp_content_type = response.headers.get("content-type", "")

        try:
            req_body = _serialize_body(request.content, req_content_type)
        except httpx.RequestNotRead:
            req_body = "[streaming request body]"
        resp_body = _serialize_body(response.content, resp_content_type)

        # Mask API keys in response bodies (from POST /admin/clients)
        if resp_body and "ccas_" in resp_body:
            import re
            resp_body = re.sub(r'ccas_[a-zA-Z0-9_-]+', 'ccas_***', resp_body)

        self.history.append(RequestResponsePair(
            request_method=request.method,
            request_url=str(request.url),
            request_headers=req_headers,
            request_body=req_body,
            response_status=response.status_code,
            response_headers=dict(response.headers),
            response_body=resp_body,
            timestamp=datetime.now(UTC).isoformat(),
        ))

    def get(self, path: str, headers: dict | None = None, **kwargs) -> httpx.Response:
        resp = self._client.get(path, headers=headers, **kwargs)
        self._record(resp.request, resp)
        return resp

    def post(
        self,
        path: str,
        headers: dict | None = None,
        json: dict | None = None,
        data: dict | None = None,
        files: dict | None = None,
        content: bytes | None = None,
        **kwargs,
    ) -> httpx.Response:
        resp = self._client.post(
            path, headers=headers, json=json, data=data, files=files,
            content=content, **kwargs,
        )
        self._record(resp.request, resp)
        return resp

    def put(self, path: str, headers: dict | None = None, json: dict | None = None, **kwargs) -> httpx.Response:
        resp = self._client.put(path, headers=headers, json=json, **kwargs)
        self._record(resp.request, resp)
        return resp

    def patch(self, path: str, headers: dict | None = None, json: dict | None = None, **kwargs) -> httpx.Response:
        resp = self._client.patch(path, headers=headers, json=json, **kwargs)
        self._record(resp.request, resp)
        return resp

    def delete(self, path: str, headers: dict | None = None, params: dict | None = None, **kwargs) -> httpx.Response:
        resp = self._client.delete(path, headers=headers, params=params, **kwargs)
        self._record(resp.request, resp)
        return resp

    @property
    def last_exchange(self) -> RequestResponsePair | None:
        return self.history[-1] if self.history else None

    def clear_history(self) -> None:
        self.history.clear()
