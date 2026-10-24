"""3-4. LM Studio client: discovery, retries, streaming, unavailable server, reconnect.

Uses StubLMStudio (a small real HTTP server imitating LM Studio) and a real closed
port. Real-server behaviour is covered in tests/integration.
"""
import asyncio

import pytest

from helpers import StubLMStudio
from mikuos.llm import LLMClient, LLMError, LLMUnavailable


async def test_discovery_prefers_loaded_preferred_model():
    with StubLMStudio() as s:
        c = LLMClient(s.base_url, model_preference=["miku"], max_retries=0)
        st = await c.connect()
        assert st.connected and st.model == "gemma miku test"
        assert st.context_length == 4096 and st.supports_tools
        await c.aclose()


async def test_no_loaded_model_does_not_jit_load():
    models = [{"id": "big", "type": "llm", "state": "not-loaded"}]
    with StubLMStudio(models=models) as s:
        c = LLMClient(s.base_url, max_retries=0)
        st = await c.connect()
        assert not st.connected and "no LLM is loaded" in st.last_error
        assert not any(r["path"].endswith("/chat/completions") for r in s.requests)
        await c.aclose()


async def test_configured_model_and_v1_fallback():
    with StubLMStudio(native=False) as s:
        c = LLMClient(s.base_url, model="other-model", max_retries=0)
        st = await c.connect()
        assert st.connected and st.model == "other-model"
        sent = [r for r in s.requests if r["path"] == "/v1/chat/completions"][0]["json"]
        assert sent["model"] == "other-model" and sent["reasoning_effort"] == "none"
        await c.aclose()


async def test_retries_on_server_errors():
    with StubLMStudio(fail_first=2) as s:
        c = LLMClient(s.base_url, max_retries=2)
        st = await c.connect()
        assert st.connected
        await c.aclose()
    with StubLMStudio(fail_first=10) as s:
        c = LLMClient(s.base_url, max_retries=1)
        st = await c.connect()
        assert not st.connected and "500" in st.last_error
        await c.aclose()


async def test_streaming_tokens_and_tool_call_deltas():
    with StubLMStudio(reply="Привет, я Мику!") as s:
        c = LLMClient(s.base_url, max_retries=0)
        await c.connect(verify_generation=False)
        tokens, result = [], None
        async for ev in c.stream_chat([{"role": "user", "content": "hi"}]):
            if ev["type"] == "token":
                tokens.append(ev["text"])
            else:
                result = ev["result"]
        assert len(tokens) > 1 and "".join(tokens) == "Привет, я Мику!" == result.content
        s.tool_call = {"name": "open_application", "arguments": '{"app_id": "notepad"}'}
        async for ev in c.stream_chat([{"role": "user", "content": "x"}], tools=[{"type": "function"}]):
            result = ev.get("result", result)
        assert result.tool_calls[0].name == "open_application"
        assert result.tool_calls[0].arguments == '{"app_id": "notepad"}'
        await c.aclose()


async def test_unreachable_server_is_reported_not_raised_on_connect(dead_url):
    c = LLMClient(dead_url, max_retries=1, connect_timeout_s=1)
    st = await c.connect()
    assert not st.connected and st.model is None and "unreachable" in st.last_error
    with pytest.raises(LLMUnavailable):
        await c.chat([{"role": "user", "content": "hi"}])
    await c.aclose()


async def test_reconnect_loop_recovers_when_server_appears():
    from conftest import free_port
    port = free_port()
    c = LLMClient(f"http://127.0.0.1:{port}/v1", max_retries=0, connect_timeout_s=1)
    st = await c.connect()
    assert not st.connected  # server not started yet
    changes = []
    c.on_status_change(lambda st: changes.append(st.connected))
    stop = asyncio.Event()
    task = asyncio.create_task(c.reconnect_loop(0.2, stop))
    await asyncio.sleep(0.3)
    with StubLMStudio(port=port):
        for _ in range(30):
            if c.status.connected:
                break
            await asyncio.sleep(0.1)
        assert c.status.connected and True in changes
        stop.set()
        await task
    await c.aclose()


async def test_connection_lost_mid_session(dead_url):
    with StubLMStudio() as s:
        c = LLMClient(s.base_url, max_retries=0, connect_timeout_s=1)
        assert (await c.connect()).connected
    # server is gone now
    with pytest.raises(LLMUnavailable):
        async for _ in c.stream_chat([{"role": "user", "content": "hi"}]):
            pass
    assert not c.status.connected
    await c.aclose()


async def test_malformed_response_raises_llm_error():
    with StubLMStudio() as s:
        c = LLMClient(s.base_url, max_retries=0)
        await c.connect(verify_generation=False)
        s.server.RequestHandlerClass.do_POST = lambda self: self._send(200, {"nope": 1})
        with pytest.raises(LLMError):
            await c.chat([{"role": "user", "content": "x"}])
        await c.aclose()


async def test_health_check_detects_server_or_model_loss():
    with StubLMStudio() as s:
        c = LLMClient(s.base_url, model_preference=["miku"], max_retries=0, connect_timeout_s=1)
        assert (await c.connect()).connected
        assert await c.health_check()
        s.models[1]["state"] = "not-loaded"  # user unloaded the model in LM Studio
        assert not await c.health_check() and not c.status.connected
        assert "no longer loaded" in c.status.last_error
        await c.aclose()
