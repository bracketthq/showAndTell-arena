"""Magento benchmark catalog state through the native REST API."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from showAndTell.applications.magento.api import MagentoClient, reset_magento, seed_magento


class State:
    application = "magento"

    def __init__(self, manifest, *, client_factory=None) -> None:
        self.manifest = manifest
        self._client_factory = client_factory
        self._block: dict[str, Any] | None = None

    def _client(self, ctx) -> MagentoClient:
        if self._client_factory is not None:
            return self._client_factory(ctx)
        return MagentoClient.from_password(
            ctx.url,
            str(ctx.secrets.get("admin_username", "admin")),
            str(ctx.secrets.get("admin_password", "admin1234")),
        )

    def reset(self, ctx) -> None:
        if self._block is not None:
            with self._client(ctx) as client:
                reset_magento(client, self._block)
        self._block = None

    def seed(self, ctx, block: Mapping[str, Any]) -> None:
        if not isinstance(block, Mapping):
            raise ValueError("magento seed block must be an object")
        # The ten WebArena shopping tasks carry descriptive metadata under a
        # historical `shopping` key. Their populated image is the seed, so the
        # application data plane has nothing to apply.
        if "category" not in block or "customer" not in block:
            self._block = None
            return
        owned = deepcopy(dict(block))
        with self._client(ctx) as client:
            reset_magento(client, owned)
            seed_magento(client, owned)
        self._block = owned

    def export(self, ctx) -> dict[str, Any]:
        if self._block is None:
            return {"products": [], "cart_items": []}
        products = []
        with self._client(ctx) as client:
            for declared in self._block.get("products", []):
                current = client.product(str(declared["sku"]))
                if current is not None:
                    products.append(current)
            cart_items = client.customer_cart_items(self._block["customer"])
        return {"products": products, "cart_items": cart_items}
