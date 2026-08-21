"""Seed benchmark-owned catalog records through Magento Open Source REST APIs."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx


class MagentoClient:
    """Small admin client covering the price-watch fixture's owned records."""

    def __init__(self, base_url: str, token: str, *,
                 client: httpx.Client | None = None) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Magento base_url must be http(s)")
        if not token:
            raise ValueError("Magento admin token is required")
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=90)
        self.headers = {"Authorization": f"Bearer {token}"}

    @classmethod
    def from_password(cls, base_url: str, username: str, password: str, *,
                      client: httpx.Client | None = None) -> "MagentoClient":
        owns_client = client is None
        http = client or httpx.Client(timeout=90)
        try:
            response = http.post(
                f"{base_url.rstrip('/')}/rest/V1/integration/admin/token",
                json={"username": username, "password": password},
            )
            response.raise_for_status()
            token = response.json()
            if not isinstance(token, str) or not token:
                raise RuntimeError("Magento did not return an admin token")
            instance = cls(base_url, token, client=http)
            instance._owns_client = owns_client
            return instance
        except Exception:
            if owns_client:
                http.close()
            raise

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "MagentoClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.client.request(
            method, f"{self.base_url}/rest/V1{path}", headers=self.headers, **kwargs)
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()

    @staticmethod
    def _single_filter(field: str, value: str) -> dict[str, str]:
        prefix = "searchCriteria[filter_groups][0][filters][0]"
        return {f"{prefix}[field]": field, f"{prefix}[value]": value,
                f"{prefix}[condition_type]": "eq"}

    def ensure_customer(self, customer: Mapping[str, Any]) -> int:
        email = str(customer["email"])
        result = self._request(
            "GET", "/customers/search", params=self._single_filter("email", email))
        rows = result.get("items", []) if isinstance(result, dict) else []
        payload = {key: value for key, value in customer.items() if key != "password"}
        payload.setdefault("website_id", 1)
        if rows:
            customer_id = int(rows[0]["id"])
            payload["id"] = customer_id
            self._request("PUT", f"/customers/{customer_id}", json={"customer": payload})
            return customer_id
        created = self._request(
            "POST", "/customers", json={"customer": payload,
                                         "password": str(customer["password"])})
        if not isinstance(created, dict) or "id" not in created:
            raise RuntimeError("Magento did not return a customer id")
        return int(created["id"])

    def ensure_category(self, category: Mapping[str, Any]) -> int:
        name = str(category["name"])
        result = self._request(
            "GET", "/categories/list", params=self._single_filter("name", name))
        rows = result.get("items", []) if isinstance(result, dict) else []
        if rows:
            return int(rows[0]["id"])
        payload = {"parent_id": 2, "name": name,
                   "is_active": bool(category.get("is_active", True)),
                   "include_in_menu": True}
        created = self._request("POST", "/categories", json={"category": payload})
        if not isinstance(created, dict) or "id" not in created:
            raise RuntimeError("Magento did not return a category id")
        return int(created["id"])

    def upsert_product(self, product: Mapping[str, Any], category_id: int) -> None:
        payload = {key: value for key, value in product.items()
                   if key not in {"external_id", "review_summary"}}
        extension = dict(payload.get("extension_attributes", {}))
        extension["category_links"] = [{"position": 0,
                                         "category_id": str(category_id)}]
        payload["extension_attributes"] = extension
        sku = quote(str(payload["sku"]), safe="")
        self._request("PUT", f"/products/{sku}", json={"product": payload})

    def delete_product(self, sku: str) -> None:
        response = self.client.delete(
            f"{self.base_url}/rest/V1/products/{quote(sku, safe='')}",
            headers=self.headers)
        if response.status_code == 404:
            return
        response.raise_for_status()

    def _customer_token(self, customer: Mapping[str, Any]) -> str:
        response = self.client.post(
            f"{self.base_url}/rest/V1/integration/customer/token",
            json={"username": str(customer["email"]),
                  "password": str(customer["password"])})
        response.raise_for_status()
        token = response.json()
        if not isinstance(token, str) or not token:
            raise RuntimeError("Magento did not return a customer token")
        return token

    def empty_customer_cart(self, customer: Mapping[str, Any]) -> None:
        headers = {"Authorization": f"Bearer {self._customer_token(customer)}"}
        response = self.client.post(
            f"{self.base_url}/rest/V1/carts/mine", headers=headers)
        response.raise_for_status()
        response = self.client.get(
            f"{self.base_url}/rest/V1/carts/mine/items", headers=headers)
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise RuntimeError("Magento returned malformed cart items")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("item_id"), int):
                raise RuntimeError("Magento cart item is missing an integer item_id")
            deleted = self.client.delete(
                f"{self.base_url}/rest/V1/carts/mine/items/{row['item_id']}",
                headers=headers)
            deleted.raise_for_status()

    def customer_cart_items(self, customer: Mapping[str, Any]) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self._customer_token(customer)}"}
        response = self.client.get(
            f"{self.base_url}/rest/V1/carts/mine/items", headers=headers)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RuntimeError("Magento returned malformed cart items")
        return rows

    def product(self, sku: str) -> dict[str, Any] | None:
        response = self.client.get(
            f"{self.base_url}/rest/V1/products/{quote(sku, safe='')}",
            headers=self.headers)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise RuntimeError("Magento returned a malformed product")
        return value


def seed_magento(client: MagentoClient, block: Mapping[str, Any]) -> None:
    category_id = client.ensure_category(block["category"])
    client.ensure_customer(block["customer"])
    for product in block.get("products", []):
        client.upsert_product(product, category_id)
    client.empty_customer_cart(block["customer"])


def reset_magento(client: MagentoClient, block: Mapping[str, Any]) -> None:
    """Remove only task-owned SKUs and clear the task customer's active cart."""
    customer = block.get("customer")
    if isinstance(customer, Mapping):
        try:
            client.empty_customer_cart(customer)
        except httpx.HTTPStatusError as exc:
            # A pristine stack may not contain the task customer yet.
            if exc.response.status_code not in {400, 401, 404}:
                raise
    for product in block.get("products", []):
        client.delete_product(str(product["sku"]))
