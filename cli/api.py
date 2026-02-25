"""HTTP clients for the CCAS API."""

from __future__ import annotations

import io
from typing import Any

import requests


class ApiError(Exception):
    """Raised on API errors."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


class _BaseApiClient:
    """Shared HTTP client foundation."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 30):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {api_key}"

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Make an HTTP request with standard error handling."""
        kwargs.setdefault("timeout", self._timeout)
        try:
            resp = self._session.request(method, self._url(path), **kwargs)
        except requests.ConnectionError:
            raise ApiError(0, f"Cannot connect to {self._base_url}. Is the server running?")
        except requests.Timeout:
            raise ApiError(0, "Request timed out.")

        if resp.status_code >= 400:
            self._handle_error(resp)

        return resp

    def _handle_error(self, resp: requests.Response) -> None:
        """Override in subclasses for custom error messages."""
        detail = self._extract_detail(resp)
        raise ApiError(resp.status_code, detail)

    @staticmethod
    def _extract_detail(resp: requests.Response) -> str:
        """Extract error detail from response."""
        try:
            body = resp.json()
            if isinstance(body, dict) and "detail" in body:
                return str(body["detail"])
        except (ValueError, KeyError):
            pass
        return resp.text[:500] if resp.text else f"HTTP {resp.status_code}"


class AdminApiClient(_BaseApiClient):
    """Client for CCAS Admin API endpoints."""

    def _handle_error(self, resp: requests.Response) -> None:
        detail = self._extract_detail(resp)
        if resp.status_code == 401:
            detail = "Authentication failed. Check your admin API key."
        elif resp.status_code == 403:
            detail = "Forbidden. Your key may not have admin role."
        raise ApiError(resp.status_code, detail)

    # --- Connection ---

    def health(self) -> dict:
        resp = self._session.get(
            self._url("/v1/health"), timeout=self._timeout
        )
        return resp.json()

    def admin_status(self) -> dict:
        return self._request("GET", "/v1/admin/status").json()

    # --- Skills ---

    def list_skills(self) -> list[dict]:
        return self._request("GET", "/v1/admin/skills").json()

    def get_skill(self, name: str) -> dict:
        return self._request("GET", f"/v1/admin/skills/{name}").json()

    def add_skill_zip(self, zip_bytes: bytes, name: str | None = None) -> dict:
        files = {"skill_data": ("skill.zip", io.BytesIO(zip_bytes), "application/zip")}
        data = {}
        if name:
            data["name"] = name
        return self._request(
            "POST", "/v1/admin/skills", files=files, data=data, timeout=60,
        ).json()

    def update_skill_zip(self, name: str, zip_bytes: bytes) -> dict:
        files = {"skill_data": ("skill.zip", io.BytesIO(zip_bytes), "application/zip")}
        return self._request(
            "PUT", f"/v1/admin/skills/{name}", files=files, timeout=60,
        ).json()

    def remove_skill(self, name: str) -> None:
        self._request("DELETE", f"/v1/admin/skills/{name}")

    # --- Agents ---

    def list_agents(self) -> list[dict]:
        return self._request("GET", "/v1/admin/agents").json()

    def get_agent(self, name: str) -> dict:
        return self._request("GET", f"/v1/admin/agents/{name}").json()

    def add_agent(self, name: str, content: str, description: str = "") -> dict:
        payload: dict[str, Any] = {"name": name, "content": content}
        if description:
            payload["description"] = description
        return self._request("POST", "/v1/admin/agents", json=payload).json()

    def update_agent(self, name: str, content: str | None = None,
                     description: str | None = None) -> dict:
        payload: dict[str, Any] = {}
        if content is not None:
            payload["content"] = content
        if description is not None:
            payload["description"] = description
        return self._request("PUT", f"/v1/admin/agents/{name}", json=payload).json()

    def remove_agent(self, name: str) -> None:
        self._request("DELETE", f"/v1/admin/agents/{name}")

    # --- MCP Servers ---

    def list_mcp(self) -> list[dict]:
        return self._request("GET", "/v1/admin/mcp").json()

    def get_mcp(self, name: str) -> dict:
        return self._request("GET", f"/v1/admin/mcp/{name}").json()

    def add_mcp(self, name: str, config: dict) -> dict:
        payload: dict[str, Any] = {"name": name, **config}
        return self._request("POST", "/v1/admin/mcp", json=payload).json()

    def install_mcp(self, package: str, name: str | None = None,
                    description: str = "", pip: bool = False) -> dict:
        payload: dict[str, Any] = {"package": package, "pip": pip}
        if name:
            payload["name"] = name
        if description:
            payload["description"] = description
        return self._request(
            "POST", "/v1/admin/mcp/install", json=payload, timeout=120,
        ).json()

    def remove_mcp(self, name: str, keep_package: bool = False) -> None:
        params = {}
        if keep_package:
            params["keep_package"] = "true"
        self._request("DELETE", f"/v1/admin/mcp/{name}", params=params)

    def health_check_all(self) -> list[dict]:
        return self._request("POST", "/v1/admin/mcp/health-check", timeout=60).json()

    def health_check(self, name: str) -> dict:
        return self._request(
            "POST", f"/v1/admin/mcp/{name}/health-check", timeout=30,
        ).json()

    # --- Clients ---

    def list_clients(self) -> list[dict]:
        return self._request("GET", "/v1/admin/clients").json()

    def get_client(self, client_id: str) -> dict:
        return self._request("GET", f"/v1/admin/clients/{client_id}").json()

    def add_client(self, client_id: str, role: str = "client",
                   description: str = "", security_profile: str = "common") -> dict:
        payload: dict[str, Any] = {
            "client_id": client_id,
            "role": role,
        }
        if description:
            payload["description"] = description
        if security_profile != "common":
            payload["security_profile"] = security_profile
        return self._request("POST", "/v1/admin/clients", json=payload).json()

    def update_client(self, client_id: str, **kwargs) -> dict:
        payload = {k: v for k, v in kwargs.items() if v is not None}
        return self._request("PATCH", f"/v1/admin/clients/{client_id}", json=payload).json()

    def remove_client(self, client_id: str) -> None:
        self._request("DELETE", f"/v1/admin/clients/{client_id}")

    def activate_client(self, client_id: str) -> dict:
        return self._request("POST", f"/v1/admin/clients/{client_id}/activate").json()

    def deactivate_client(self, client_id: str) -> dict:
        return self._request("POST", f"/v1/admin/clients/{client_id}/deactivate").json()

    # --- Security Profiles ---

    def list_profiles(self) -> list[dict]:
        return self._request("GET", "/v1/admin/security-profiles").json()

    def get_profile(self, name: str) -> dict:
        return self._request("GET", f"/v1/admin/security-profiles/{name}").json()

    def add_profile(self, name: str, **kwargs) -> dict:
        payload: dict[str, Any] = {"name": name, **kwargs}
        return self._request("POST", "/v1/admin/security-profiles", json=payload).json()

    def update_profile(self, name: str, **kwargs) -> dict:
        payload = {k: v for k, v in kwargs.items() if v is not None}
        return self._request("PATCH", f"/v1/admin/security-profiles/{name}", json=payload).json()

    def remove_profile(self, name: str) -> None:
        self._request("DELETE", f"/v1/admin/security-profiles/{name}")

    def set_default_profile(self, name: str) -> dict:
        return self._request("POST", f"/v1/admin/security-profiles/{name}/set-default").json()


class JobApiClient(_BaseApiClient):
    """Client for CCAS Job API endpoints."""

    def _handle_error(self, resp: requests.Response) -> None:
        detail = self._extract_detail(resp)
        if resp.status_code == 401:
            detail = "Authentication failed. Check your API key."
        elif resp.status_code == 404:
            detail = self._extract_detail(resp) or "Not found."
        elif resp.status_code == 413:
            detail = "File too large (max 50MB)."
        elif resp.status_code == 429:
            detail = "Too many pending jobs. Try again later."
        raise ApiError(resp.status_code, detail)

    def upload(self, zip_bytes: bytes) -> str:
        """Upload a ZIP archive. Returns upload_id."""
        resp = self._request(
            "POST",
            "/v1/uploads",
            files={"file": ("upload.zip", io.BytesIO(zip_bytes), "application/zip")},
            timeout=120,
        )
        data = resp.json()
        return data["upload_id"]

    def create_job(
        self,
        prompt: str | None,
        anthropic_key: str,
        upload_ids: list[str] | None = None,
        agent: str | None = None,
        model: str | None = None,
        timeout_seconds: int | None = None,
        claude_md: str | None = None,
    ) -> dict:
        """Create a job. Returns the job creation response dict."""
        payload: dict[str, Any] = {}
        if prompt is not None:
            payload["prompt"] = prompt
        if upload_ids:
            payload["upload_ids"] = upload_ids
        if agent:
            payload["agent"] = agent
        if model:
            payload["model"] = model
        if timeout_seconds is not None:
            payload["timeout_seconds"] = timeout_seconds
        if claude_md is not None:
            payload["claude_md"] = claude_md

        resp = self._request(
            "POST",
            "/v1/jobs",
            json=payload,
            headers={"X-Anthropic-Key": anthropic_key},
            timeout=self._timeout,
        )
        return resp.json()

    def get_job(self, job_id: str) -> dict:
        """Get job status and results. Returns the full job dict."""
        return self._request("GET", f"/v1/jobs/{job_id}").json()
