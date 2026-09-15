from __future__ import annotations

import json

import httpx

from showAndTell.applications.erpnext.api import ERPNextClient, ResourceRef


def test_erpnext_client_uses_real_frappe_resource_endpoints():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"data": {"name": "ITEM-001"}})
        if request.method == "GET":
            return httpx.Response(200, json={"data": {"name": "ITEM-001"}})
        return httpx.Response(200, json={"data": {}})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http:
        client = ERPNextClient("http://erpnext.local", "key:secret", client=http)
        ref = client.create("Item", {"item_code": "ITEM-001"})
        assert ref == ResourceRef("Item", "ITEM-001")
        assert client.get(ref)["name"] == "ITEM-001"
        client.update(ref, {"item_name": "Fixture-free item"})
        client.delete(ref)

    assert [request.method for request in requests] == ["POST", "GET", "PUT", "DELETE"]
    assert requests[0].url.path == "/api/resource/Item"
    assert requests[1].url.path == "/api/resource/Item/ITEM-001"
    assert requests[0].headers["authorization"] == "token key:secret"


def test_erpnext_client_can_use_the_real_password_login_session():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/method/login":
            return httpx.Response(
                200,
                json={"message": "Logged In"},
                headers={"set-cookie": "sid=test-session; Path=/"},
            )
        assert request.headers.get("cookie") == "sid=test-session"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"data": {"name": "ITEM-001"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with ERPNextClient.password_login(
            "http://erpnext.local", "Administrator", "showAndTell-admin", client=http
        ) as client:
            assert client.get(ResourceRef("Item", "ITEM-001"))["name"] == "ITEM-001"

    assert [request.url.path for request in requests] == [
        "/api/method/login",
        "/api/resource/Item/ITEM-001",
    ]


def test_erpnext_client_lists_every_page_with_requested_native_fields():
    requests: list[httpx.Request] = []
    pages = {
        "0": [
            {"name": "HR-EMP-0001", "attendance_device_id": "1001"},
            {"name": "HR-EMP-0002", "attendance_device_id": "1002"},
        ],
        "2": [
            {"name": "HR-EMP-0003", "attendance_device_id": "1003"},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"data": pages[request.url.params["limit_start"]]},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = ERPNextClient("http://erpnext.local", "key:secret", client=http)
        rows = client.list_all(
            "Employee",
            fields=("name", "attendance_device_id"),
            page_length=2,
        )

    assert rows == pages["0"] + pages["2"]
    assert [request.url.params["limit_start"] for request in requests] == ["0", "2"]
    assert all(
        request.url.params["fields"] == json.dumps(
            ["name", "attendance_device_id"], separators=(",", ":")
        )
        for request in requests
    )
