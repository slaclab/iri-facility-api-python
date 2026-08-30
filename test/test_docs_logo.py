#!/usr/bin/env python3
"""Regression tests for the Swagger UI logo route."""

from fastapi.testclient import TestClient

from app.main import app


def test_openapi_description_uses_absolute_logo_route():
    client = TestClient(app)

    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert (
        '<img src="/logo/SLAC_primary_red.png" height=100 />'
        in response.json()["info"]["description"]
    )


def test_logo_route_serves_png():
    client = TestClient(app)

    response = client.get("/logo/SLAC_primary_red.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
