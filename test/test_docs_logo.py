#!/usr/bin/env python3
"""Regression tests for the Swagger UI logo route."""

from fastapi.testclient import TestClient

from app import config
from app.main import app


def test_openapi_description_uses_api_logo_route():
    client = TestClient(app)

    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert (
        f'<img src="{config.DOCS_LOGO_URL}" height=100 />'
        in response.json()["info"]["description"]
    )


def test_api_logo_route_serves_png():
    client = TestClient(app)

    response = client.get(config.DOCS_LOGO_URL)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_root_logo_route_remains_available():
    client = TestClient(app)

    response = client.get("/logo/SLAC_primary_red.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_docs_logo_url_preserves_configured_prefix():
    assert (
        config.docs_logo_url("/esnet-east/", "/api/v1/")
        == "/esnet-east/api/v1/logo/SLAC_primary_red.png"
    )
    assert config.docs_logo_route("/esnet-east/", "/api/v1/") == "/esnet-east/api/v1/logo"
