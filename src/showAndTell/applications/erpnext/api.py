"""Small HTTP adapter for the real ERPNext/Frappe REST API.

It intentionally exposes ERPNext resources rather than emulating SAP screens.
Authentication accepts the normal Frappe ``token api_key:api_secret`` value.
Family-specific profile translation can build on these tested primitives.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping
from urllib.parse import quote

import httpx


@dataclass(frozen=True)
class ResourceRef:
    doctype: str
    name: str


class ERPNextClient:
    def __init__(self, base_url: str, api_token: str | None, *,
                 client: httpx.Client | None = None) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("ERPNext base_url must be http(s)")
        if api_token is not None and not api_token.strip():
            raise ValueError("ERPNext API token cannot be blank")
        if api_token is None and client is None:
            raise ValueError("session authentication requires an HTTP client")
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=60)
        self.headers = (
            {"Authorization": f"token {api_token}"} if api_token is not None else {}
        )

    @classmethod
    def password_login(
        cls,
        base_url: str,
        username: str,
        password: str,
        *,
        client: httpx.Client | None = None,
    ) -> "ERPNextClient":
        """Authenticate through Frappe's normal session-login endpoint.

        API tokens remain the preferred option for long-running deployments.
        A disposable benchmark site starts with only the Administrator password,
        however, so the local Compose pilot uses a cookie-backed session without
        manufacturing credentials through SQL or a substitute API.
        """
        if not username or not password:
            raise ValueError("ERPNext username and password are required")
        owns_client = client is None
        session = client or httpx.Client(timeout=60)
        try:
            response = session.post(
                f"{base_url.rstrip('/')}/api/method/login",
                data={"usr": username, "pwd": password},
            )
            response.raise_for_status()
        except Exception:
            if owns_client:
                session.close()
            raise
        result = cls(base_url, None, client=session)
        result._owns_client = owns_client
        return result

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "ERPNextClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _resource_url(self, doctype: str, name: str | None = None) -> str:
        url = f"{self.base_url}/api/resource/{quote(doctype, safe='')}"
        return f"{url}/{quote(name, safe='')}" if name else url

    def create(self, doctype: str, values: Mapping[str, Any]) -> ResourceRef:
        response = self.client.post(self._resource_url(doctype), headers=self.headers,
                                    json=dict(values))
        response.raise_for_status()
        data = response.json().get("data", {})
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise RuntimeError(f"ERPNext did not return a name for {doctype}")
        return ResourceRef(doctype, name)

    def find_one(self, doctype: str, filters: Mapping[str, Any]) -> ResourceRef | None:
        """Return the first resource matching Frappe list filters."""
        response = self.client.get(
            self._resource_url(doctype),
            headers=self.headers,
            params={
                "filters": json.dumps(dict(filters), separators=(",", ":")),
                "fields": json.dumps(["name"]),
                "limit_page_length": "1",
            },
        )
        response.raise_for_status()
        data = response.json().get("data")
        if not isinstance(data, list):
            raise RuntimeError(f"ERPNext returned malformed list data for {doctype}")
        if not data:
            return None
        name = data[0].get("name") if isinstance(data[0], dict) else None
        if not isinstance(name, str) or not name:
            raise RuntimeError(f"ERPNext returned an unnamed {doctype} record")
        return ResourceRef(doctype, name)

    def list_all(
        self,
        doctype: str,
        *,
        fields: tuple[str, ...] = ("name",),
        page_length: int = 200,
    ) -> list[dict[str, Any]]:
        """Return a validated, paginated resource list.

        This is primarily for resolving a bounded base dataset from stable
        native fields in one pass. It also avoids relying on freshly-created
        custom fields in Frappe's per-worker report-view cache.
        """
        if not fields or not 1 <= page_length <= 500:
            raise ValueError("ERPNext list_all needs fields and page_length 1..500")
        rows: list[dict[str, Any]] = []
        start = 0
        while True:
            response = self.client.get(
                self._resource_url(doctype),
                headers=self.headers,
                params={
                    "fields": json.dumps(list(fields), separators=(",", ":")),
                    "limit_start": str(start),
                    "limit_page_length": str(page_length),
                },
            )
            response.raise_for_status()
            page = response.json().get("data")
            if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                raise RuntimeError(f"ERPNext returned malformed list data for {doctype}")
            rows.extend(page)
            if len(page) < page_length:
                return rows
            start += len(page)

    def ensure(self, doctype: str, natural_key: Mapping[str, Any],
               values: Mapping[str, Any]) -> ResourceRef:
        """Create a master record or update the existing natural-key match.

        This is intended for master/configuration DocTypes. Submitted business
        transactions are intentionally never upserted; fixture reset must restore
        a clean site snapshot before those are seeded again.
        """
        existing = self.find_one(doctype, natural_key)
        if existing is None:
            return self.create(doctype, values)
        self.update(existing, values)
        return existing

    def update(self, ref: ResourceRef, values: Mapping[str, Any]) -> None:
        response = self.client.put(self._resource_url(ref.doctype, ref.name),
                                   headers=self.headers, json=dict(values))
        response.raise_for_status()

    def delete(self, ref: ResourceRef) -> None:
        response = self.client.delete(self._resource_url(ref.doctype, ref.name),
                                      headers=self.headers)
        response.raise_for_status()

    def get(self, ref: ResourceRef) -> dict[str, Any]:
        response = self.client.get(self._resource_url(ref.doctype, ref.name),
                                   headers=self.headers)
        response.raise_for_status()
        data = response.json().get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"ERPNext returned malformed data for {ref}")
        return data
