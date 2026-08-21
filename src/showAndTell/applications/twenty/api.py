"""Strict synchronous client for Twenty v2.8.5's public Core REST API."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Self
import httpx

from showAndTell.applications.twenty import validate


RESOURCE_SINGULAR = {
    "companies": "Company",
    "people": "Person",
    "opportunities": "Opportunity",
    "tasks": "Task",
}


class PrivateToken:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value or any(ch.isspace() for ch in value):
            raise ValueError("Twenty API token must be a non-empty exact string")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "PrivateToken(<redacted>)"

    __str__ = __repr__


class TwentyError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        method: str = "",
        endpoint: str = "",
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.method = method
        self.endpoint = endpoint
        self.outcome_unknown = outcome_unknown


def _base_url(value: str) -> str:
    return validate.base_url(value, "Twenty base URL")


class TwentyClient:
    def __init__(
        self,
        base_url: str,
        token: str | PrivateToken,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = _base_url(base_url)
        self.token = token if isinstance(token, PrivateToken) else PrivateToken(token)
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=60.0)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _resource(self, value: str) -> str:
        if value not in RESOURCE_SINGULAR:
            raise ValueError(f"unsupported Twenty resource {value!r}")
        return value

    def _diagnostic(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
            text = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        except (ValueError, TypeError):
            text = response.text
        return text.replace(self.token.reveal(), "[redacted]")[:700]

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.token.reveal()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "showAndTell-twenty-fixture/1",
        }
        try:
            response = self.client.request(
                method,
                f"{self.base_url}{endpoint}",
                headers=headers,
                json=dict(json_body) if json_body is not None else None,
                params=params,
            )
        except httpx.HTTPError as exc:
            raise TwentyError(
                f"Twenty {method} {endpoint} did not return a response",
                method=method,
                endpoint=endpoint,
                outcome_unknown=method != "GET",
            ) from exc
        if not 200 <= response.status_code < 300:
            raise TwentyError(
                f"Twenty {method} {endpoint} failed with HTTP "
                f"{response.status_code}: {self._diagnostic(response)}",
                status_code=response.status_code,
                method=method,
                endpoint=endpoint,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TwentyError(
                f"Twenty {method} {endpoint} returned non-JSON data",
                status_code=response.status_code,
                method=method,
                endpoint=endpoint,
            ) from exc
        if not isinstance(payload, dict):
            raise TwentyError(f"Twenty {method} {endpoint} returned malformed JSON")
        # The REST facade is GraphQL-backed and may expose an errors array with
        # a non-null data sibling. Never accept that as a successful mutation.
        if payload.get("errors"):
            raise TwentyError(
                f"Twenty {method} {endpoint} returned application errors: "
                f"{self._diagnostic(response)}",
                status_code=response.status_code,
                method=method,
                endpoint=endpoint,
            )
        return payload

    @staticmethod
    def _record(payload: Mapping[str, Any], operation: str) -> dict[str, Any]:
        data = payload.get("data")
        row = data.get(operation) if isinstance(data, Mapping) else None
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise TwentyError(f"Twenty returned malformed {operation} data")
        return row

    def create(self, resource: str, values: Mapping[str, Any]) -> dict[str, Any]:
        resource = self._resource(resource)
        payload = self._request("POST", f"/rest/{resource}", json_body=values)
        return self._record(payload, f"create{RESOURCE_SINGULAR[resource]}")

    def update(
        self, resource: str, record_id: str, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        resource = self._resource(resource)
        if not isinstance(record_id, str) or not record_id:
            raise ValueError("Twenty record id must be a non-empty string")
        payload = self._request(
            "PATCH", f"/rest/{resource}/{record_id}", json_body=values
        )
        return self._record(payload, f"update{RESOURCE_SINGULAR[resource]}")

    def delete(self, resource: str, record_id: str) -> dict[str, Any]:
        resource = self._resource(resource)
        if not isinstance(record_id, str) or not record_id:
            raise ValueError("Twenty record id must be a non-empty string")
        payload = self._request("DELETE", f"/rest/{resource}/{record_id}")
        return self._record(payload, f"delete{RESOURCE_SINGULAR[resource]}")

    def list_all(self, resource: str, *, limit: int = 100) -> list[dict[str, Any]]:
        resource = self._resource(resource)
        if not 1 <= limit <= 200:
            raise ValueError("Twenty page limit must be between 1 and 200")
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            params: dict[str, Any] = {"limit": limit}
            if cursor is not None:
                params["starting_after"] = cursor
            payload = self._request("GET", f"/rest/{resource}", params=params)
            data = payload.get("data")
            page = data.get(resource) if isinstance(data, Mapping) else None
            page_info = payload.get("pageInfo")
            if (
                not isinstance(page, list)
                or any(not isinstance(row, dict) for row in page)
                or not isinstance(page_info, Mapping)
            ):
                raise TwentyError(f"Twenty returned malformed {resource} page")
            for row in page:
                record_id = row.get("id")
                if not isinstance(record_id, str) or record_id in seen:
                    raise TwentyError(f"Twenty returned duplicate or malformed {resource} id")
                seen.add(record_id)
                rows.append(row)
            if page_info.get("hasNextPage") is False:
                return rows
            next_cursor = page_info.get("endCursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                raise TwentyError(f"Twenty returned a non-advancing {resource} cursor")
            cursor = next_cursor
