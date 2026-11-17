"""HTTP client for the RAG backend service (reconstructed from bytecode)."""
from __future__ import annotations

import json
import time
from typing import Any

import httpx


class RagClient:
    """Thin HTTP client for the RAG service.

    All methods return plain dicts (parsed JSON) and raise on unrecoverable
    errors. Network timeouts are retried with exponential backoff.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8765", timeout: float = 30.0):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def _close_client(self) -> None:
        """Safely close and null out the persistent HTTP client."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def _post(self, path: str, payload: dict | None = None, retries: int = 3) -> dict[str, Any]:
        """POST with retry on connection errors.

        A *read* timeout is NOT retried. Connection errors are retried with backoff.
        """
        url = f"{self.base_url}{path}"
        last_err: Exception | None = None
        for attempt in range(max(1, retries)):
            try:
                resp = self.client.post(url, json=payload or {})
                if resp.status_code >= 400:
                    body = (resp.text or "").strip() or "(empty body)"
                    raise httpx.HTTPStatusError(
                        f"Backend HTTP {resp.status_code}: {body}",
                        request=resp.request,
                        response=resp,
                    )
                if not (resp.content or b"").strip():
                    raise ValueError(
                        "Backend returned empty response body (service may have crashed or been killed)"
                    )
                try:
                    return resp.json()
                except json.JSONDecodeError as e:
                    raise ValueError(
                        f"RAG service returned invalid response after {attempt + 1} attempts: {e}"
                    ) from e
            except (httpx.ReadTimeout, TimeoutError) as e:
                raise TimeoutError(
                    f"RAG service did not respond within {self.timeout:.0f}s (request timed out): {e}"
                ) from e
            except (
                ConnectionError,
                httpx.ConnectError,
                httpx.WriteTimeout,
                OSError,
            ) as e:
                last_err = e
                self._close_client()
                time.sleep(0.4 * (2 ** attempt))
                continue
        raise ConnectionError(
            f"RAG service unreachable after {retries} attempts: {last_err}"
        )

    def _get(self, path: str, retries: int = 3) -> dict[str, Any]:
        """GET with retry."""
        url = f"{self.base_url}{path}"
        last_err: Exception | None = None
        for attempt in range(max(1, retries)):
            try:
                resp = self.client.get(url)
                if resp.status_code >= 400:
                    body = (resp.text or "").strip() or "(empty body)"
                    raise httpx.HTTPStatusError(
                        f"Backend HTTP {resp.status_code}: {body}",
                        request=resp.request,
                        response=resp,
                    )
                if not (resp.content or b"").strip():
                    raise ValueError("Backend returned empty response body")
                try:
                    return resp.json()
                except json.JSONDecodeError as e:
                    raise ValueError(f"RAG service invalid response: {e}") from e
            except (
                ConnectionError,
                httpx.ConnectError,
                httpx.ReadTimeout,
                OSError,
            ) as e:
                last_err = e
                self._close_client()
                time.sleep(0.4 * (2 ** attempt))
                continue
        raise ConnectionError(f"RAG service unreachable: {last_err}")

    def health(self) -> dict[str, Any]:
        """Check if the RAG service is alive."""
        return self._get("/health")

    def is_alive(self, timeout: float = 2.0) -> bool:
        """Quick, BOUNDED liveness check (localhost)."""
        try:
            with httpx.Client(timeout=timeout) as c:
                r = c.get(f"{self.base_url}/health")
                return r.status_code == 200
        except Exception:
            return False

    def status(self, timeout: float = 2.0) -> dict[str, Any] | None:
        """Return KB readiness state from GET /status (None if unreachable)."""
        try:
            with httpx.Client(timeout=timeout) as c:
                r = c.get(f"{self.base_url}/status")
                return r.json()
        except Exception:
            return None

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        scope: str | None = "shared",
        user_id: str | None = None,
        filters: dict | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query, "n": top_k}
        if scope is not None:
            payload["scope"] = scope
        if user_id is not None:
            payload["user_id"] = user_id
        if filters:
            payload["filters"] = filters
        return self._post("/query", payload)

    def ask(
        self,
        query: str,
        history: list[dict] | None = None,
        n: int = 6,
        scope: str | None = "shared",
        force_retrieve: bool = False,
        filter_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "query": query,
            "history": history or [],
            "n": n,
            "force_retrieve": force_retrieve,
        }
        if scope is not None:
            payload["scope"] = scope
        if filter_paths:
            payload["filter_paths"] = filter_paths
        # Long LLM calls need a larger timeout
        old = self.timeout
        self.timeout = max(old, 180.0)
        self._close_client()
        try:
            return self._post("/ask", payload, retries=1)
        finally:
            self.timeout = old
            self._close_client()

    def ingest_emails(self, json_path: str, user_id: str = "shared", scope: str = "shared") -> dict:
        return self._post(
            "/ingest/emails",
            {"json_path": json_path, "user_id": user_id, "scope": scope},
        )

    def ingest_directory(self, dir_path: str, user_id: str = "shared", scope: str = "shared") -> dict:
        return self._post(
            "/ingest/dir",
            {"dir": dir_path, "user_id": user_id, "scope": scope},
            retries=1,
        )

    def ingest_files(self, paths: list[str], user_id: str = "shared", scope: str = "shared") -> dict:
        return self._post(
            "/ingest/files",
            {"paths": paths, "user_id": user_id, "scope": scope},
        )

    def remove_from_db(self, paths: list[str], user_id: str = "shared", scope: str = "shared") -> dict:
        return self._post(
            "/remove",
            {"paths": paths, "user_id": user_id, "scope": scope},
        )

    def remove_folder_from_db(
        self, folder_rel: str, user_id: str = "shared", scope: str = "shared"
    ) -> dict:
        return self._post(
            "/remove/folder",
            {"folder": folder_rel, "user_id": user_id, "scope": scope},
        )

    def update_config(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        auto_retrieve: bool | None = None,
        **_ignored,
    ) -> dict:
        payload: dict[str, Any] = {}
        if base_url:
            payload["base_url"] = base_url
        if api_key:
            payload["api_key"] = api_key
        if model:
            payload["model"] = model
        if auto_retrieve is not None:
            payload["auto_retrieve"] = bool(auto_retrieve)
        return self._post("/config", payload)

    def generate_report(
        self,
        json_path: str,
        output_dir: str,
        holdings_csv: str | None = None,
        language: str = "zh",
        date: str | None = None,
        timeout: float = 600.0,
    ) -> dict:
        payload: dict[str, Any] = {
            "json_path": json_path,
            "output_dir": output_dir,
            "language": language,
        }
        if holdings_csv:
            payload["holdings_csv"] = holdings_csv
        if date:
            payload["date"] = date
        try:
            with httpx.Client(timeout=timeout) as cli:
                resp = cli.post(f"{self.base_url}/generate-report", json=payload)
                return resp.json()
        except Exception:
            return self._post("/generate-report", payload, retries=1)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
