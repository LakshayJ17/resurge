"""Offline tests for the core engine: JSON handling, text tools, rate limits, routing, gateway."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from resurge.budget import Budget
from resurge.config import Settings
from resurge.llm import LLMGateway, LLMModel, Registry, extract_json, schema_hint
from resurge.llm.jsonutil import coerce_root
from resurge.llm.ratelimit import RateLimiter
from resurge.llm.registry import ModelSpec, ProviderSpec, RoleSpec
from resurge.schemas import Depth
from resurge.storage import Storage
from resurge.text import BM25, GroundingIndex, chunk_text, estimate_tokens


# ----------------------------------------------------------------- JSON utils
class Item(LLMModel):
    text: str
    fact_ids: list[str] = []


class Out(LLMModel):
    items: list[Item] = []
    note: str = ""


def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('<think>hmm {"x":0}</think> Sure! {"a": [1, 2]} done') == {"a": [1, 2]}
    assert extract_json("{'a': 1, 'b': [1,2,],}") == {"a": 1, "b": [1, 2]}  # repaired
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_forgiving_model_and_root_coercion():
    o = Out.model_validate(coerce_root(Out, [{"text": "a", "fact_ids": None}, "b"]))
    assert [i.text for i in o.items] == ["a", "b"] and o.items[0].fact_ids == []
    o2 = Out.model_validate({"items": "single", "note": ["x", "y"]})
    assert o2.items[0].text == "single" and o2.note == "x; y"
    o3 = Out.model_validate(coerce_root(Out, {"result": {"items": [], "note": "n"}}))
    assert o3.note == "n"


def test_schema_hint_compact():
    hint = schema_hint(Out)
    assert '"items": [{"text": string, "fact_ids": [string, ...]}, ...]' in hint
    assert len(hint) < 400


# ----------------------------------------------------------------- text
def test_chunk_bm25_grounding():
    text = "\n\n".join([
        "Acme Pro costs $12 per user per month billed annually. Enterprise pricing is custom.",
        "Acme was founded in 2015 and is headquartered in Berlin with 300 employees.",
        "Customers praise the editor but complain that mobile sync is slow.",
    ] * 3)
    chunks = chunk_text(text, max_chars=200, overlap=0, min_chars=10)
    assert len(chunks) >= 3
    bm = BM25(chunks)
    top = bm.top_k("pricing per user month", k=1)
    assert "$12" in chunks[top[0]]
    g = GroundingIndex(chunks[top[0]])
    assert g.support("Acme Pro costs $12 per user per month") == 1.0
    assert g.support("Acme Pro costs $99 per seat each week with discounts") < 0.6
    assert estimate_tokens("x" * 400) == 100


# ----------------------------------------------------------------- registry/limits
def _registry(tpm=None, rpm=2, rpd=3) -> Registry:
    providers = {"fake": ProviderSpec(name="fake", base_url="https://fake.test/v1", api_key_env=["FAKE_KEY"],
                                      concurrency=2, rpd=None),
                 "other": ProviderSpec(name="other", base_url="https://other.test/v1", api_key_env=["FAKE_KEY"])}
    models = {}
    for m in (ModelSpec(provider="fake", name="big", family="alpha", tags={"analysis", "verifier"}, quality=0.9,
                        rpm=rpm, rpd=rpd, tpm=tpm),
              ModelSpec(provider="other", name="small", family="beta", tags={"analysis", "verifier"}, quality=0.6)):
        models[m.id] = m
    roles = {"analyst": RoleSpec(name="analyst", prefer=["fake/big"], tags=["analysis"], max_tokens=500),
             "checker": RoleSpec(name="checker", prefer=[], tags=["verifier"], max_tokens=300, cross_family=True)}
    return Registry(providers, models, roles, {"input": 3, "output": 15})


def test_rate_limiter_windows_and_daily(monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    reg = _registry(tpm=1000, rpm=2, rpd=3)
    st = Storage(":memory:")
    lim = RateLimiter(st, reg)
    m = reg.models["fake/big"]

    async def go():
        assert await lim.acquire(m, 100, max_wait=0.1)
        lim.commit(m, 100, 100)
        assert await lim.acquire(m, 100, max_wait=0.1)
        lim.commit(m, 100, 100)
        assert not await lim.acquire(m, 100, max_wait=0.1)  # RPM=2 reached
        assert lim.wait_needed(m, 5000) == float("inf")  # can never fit TPM
    asyncio.run(go())
    lim.storage.add_usage(f"m:{m.id}", lim.day(reg.providers["fake"]), 1, 0)
    assert lim.daily_exhausted(m)  # RPD=3 reached


def test_router_prefers_and_avoids_family(monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    reg = _registry()
    st = Storage(":memory:")
    gw = LLMGateway(reg, st, Settings(), budget=Budget(Depth.quick))
    assert [m.id for m in gw.candidates("analyst", 100)][0] == "fake/big"
    assert [m.id for m in gw.candidates("checker", 100, avoid_families={"alpha"})][0] == "other/small"
    st.set_disabled("fake/big", time.time() + 60, "test")
    assert "fake/big" not in [m.id for m in gw.candidates("analyst", 100)]
    asyncio.run(gw.aclose())


# ----------------------------------------------------------------- gateway e2e (mock HTTP)
def _mock_gateway(handler, monkeypatch) -> LLMGateway:
    monkeypatch.setenv("FAKE_KEY", "k")
    reg = _registry(rpm=100, rpd=100)
    gw = LLMGateway(reg, Storage(":memory:"), Settings(max_wait_s=0.2), budget=Budget(Depth.quick), run_id="t")
    gw.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return gw


def _ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                                     "usage": {"prompt_tokens": 50, "completion_tokens": 20}})


def test_gateway_failover_on_429_and_json_repair(monkeypatch):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body["model"])
        if body["model"] == "big":
            return httpx.Response(429, headers={"retry-after": "30"}, json={"error": {"message": "Rate limit reached"}})
        if len(calls) == 2:
            return _ok('Here you go: {"items": [{"text": "x", "fact_ids": ["F-1"]}], "note": }')  # broken -> repaired
        return _ok('{"items": [{"text": "y"}], "note": "ok"}')

    gw = _mock_gateway(handler, monkeypatch)
    obj, resp = asyncio.run(gw.complete_json(role="analyst", system="s", user="u", schema=Out))
    assert resp.model_id == "other/small" and obj.items[0].text in ("x", "y")
    assert calls[0] == "big"  # tried preferred first, then failed over
    asyncio.run(gw.aclose())


def test_gateway_adapts_json_mode_and_caches(monkeypatch):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append("response_format" in body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "response_format json_object is not supported"}})
        return _ok('{"items": [], "note": "adapted"}')

    gw = _mock_gateway(handler, monkeypatch)
    obj, resp = asyncio.run(gw.complete_json(role="analyst", system="s", user="u2", schema=Out))
    assert obj.note == "adapted" and seen[:2] == [True, False]
    assert gw.storage.get_health("fake/big").get("json_mode") == 0  # learned persistently
    obj2, resp2 = asyncio.run(gw.complete_json(role="analyst", system="s", user="u2", schema=Out))
    assert resp2.cached and obj2.note == "adapted"
    asyncio.run(gw.aclose())


def test_gateway_semantic_validator_retries(monkeypatch):
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return _ok('{"items": [], "note": ""}' if n["i"] == 1 else '{"items": [{"text": "z"}], "note": "fixed"}')

    gw = _mock_gateway(handler, monkeypatch)
    obj, _ = asyncio.run(gw.complete_json(role="analyst", system="s", user="u3", schema=Out,
                                          validator=lambda o: [] if o.items else ["items must not be empty"]))
    assert obj.note == "fixed" and n["i"] == 2
    asyncio.run(gw.aclose())


def test_storage_learning_primitives():
    st = Storage(":memory:")
    st.update_source("example.com", "supported")
    st.update_source("example.com", "unsupported")
    assert 0.4 < st.source_reliability("example.com", 0.5) < 0.6
    st.upsert_lesson("L-1", "fact_checker", "Check the subject name matches.", "r1")
    st.lesson_feedback(["L-1"], helpful=2)
    assert st.get_lessons("fact_checker")[0]["helpful"] == 2
    st.set_scores("r1", {"quality_score": 70})
    assert st.score_history()[-1] == ("r1", 70.0)
