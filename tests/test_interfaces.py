"""MCP server + web dashboard smoke tests (no network)."""

from __future__ import annotations

import pytest


async def test_mcp_server_exposes_tools():
    from resurge.mcp_server import build_server

    server = build_server()
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert {"research_competitors", "discover_competitors", "list_runs", "get_report_section",
            "get_battlecard", "search_knowledge_base"} <= names
    research = next(t for t in tools if t.name == "research_competitors")
    assert "target" in str(research.input_schema)


def test_web_dashboard_endpoints(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("RESURGE_HOME", str(tmp_path))
    from resurge.web.app import create_app

    client = TestClient(create_app(), base_url="http://127.0.0.1:8765")  # the dashboard answers local names only
    r = client.get("/")
    assert r.status_code == 200 and "Resurge" in r.text and "cdn" not in r.text.lower()
    assert client.get("/api/runs").json() == []
    prov = client.get("/api/providers").json()
    assert any(p["provider"] == "groq" for p in prov)
    assert client.get("/api/learn").json()["lessons"] == []
    assert client.get("/files/nope/report.html").status_code == 404
