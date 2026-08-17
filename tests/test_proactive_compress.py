from __future__ import annotations

import asyncio
import copy
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.agent.message import ImageURLPart, Message, TextPart
from astrbot.core.provider.entities import LLMResponse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
plugin_module = importlib.import_module("main")


def _dump(role: str, content, **kwargs) -> dict:
    return Message(role=role, content=content, **kwargs).model_dump()


def make_history(rounds: int = 4, size: int = 1000) -> list[dict]:
    history: list[dict] = []
    for i in range(rounds):
        history.append(_dump("user", f"user-{i}-" + ("u" * size)))
        history.append(_dump("assistant", f"assistant-{i}-" + ("a" * size)))
    return history


class FakeConversationManager:
    def __init__(self, history: list[dict], cid: str = "cid-1") -> None:
        self.cid = cid
        self.history = copy.deepcopy(history)
        self.update_calls = 0

    async def get_curr_conversation_id(self, unified_msg_origin: str) -> str:
        return self.cid

    async def get_conversation(
        self,
        unified_msg_origin: str,
        conversation_id: str,
        create_if_not_exists: bool = False,
    ):
        if conversation_id != self.cid:
            return None
        return SimpleNamespace(
            cid=self.cid,
            history=json.dumps(self.history, ensure_ascii=False),
        )

    async def update_conversation(
        self,
        unified_msg_origin: str,
        conversation_id: str | None = None,
        history: list[dict] | None = None,
        **kwargs,
    ) -> None:
        assert conversation_id == self.cid
        assert history is not None
        self.history = copy.deepcopy(history)
        self.update_calls += 1

    def append(self, *messages: dict) -> None:
        self.history.extend(copy.deepcopy(messages))


class FakeProvider:
    def __init__(
        self,
        *,
        summary: str = "summary-ok",
        max_context_tokens: int = 100_000,
        wait_for_release: bool = False,
    ) -> None:
        self.provider_config = {
            "id": "compressor",
            "model": "fake-model",
            "modalities": ["text"],
            "max_context_tokens": max_context_tokens,
        }
        self.summary = summary
        self.calls = 0
        self.last_kwargs = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not wait_for_release:
            self.release.set()

    async def text_chat(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        self.started.set()
        await self.release.wait()
        return LLMResponse(role="assistant", completion_text=self.summary)

    def get_model(self) -> str:
        return "fake-model"


class FakeContext:
    def __init__(
        self,
        conversation_manager: FakeConversationManager,
        provider: FakeProvider,
    ) -> None:
        self.conversation_manager = conversation_manager
        self.provider = provider

    def get_config(self, umo: str | None = None) -> dict:
        return {
            "provider_settings": {
                "llm_compress_provider_id": "compressor",
                "default_provider_id": "compressor",
            }
        }

    def get_provider_by_id(self, provider_id: str):
        if provider_id == "compressor":
            return self.provider
        return None


def make_plugin(
    *,
    history: list[dict] | None = None,
    provider: FakeProvider | None = None,
    config: dict | None = None,
):
    manager = FakeConversationManager(history or make_history())
    provider = provider or FakeProvider()
    context = FakeContext(manager, provider)
    merged_config = {
        "enabled": True,
        "trigger_ratio": 0.6,
        "min_messages": 2,
        "cooldown_seconds": 300,
        "retry_cooldown_seconds": 30,
        "check_delay_seconds": 0,
        "keep_recent_ratio": 0.15,
        **(config or {}),
    }
    plugin = plugin_module.ProactiveCompressPlugin(context, merged_config)
    return plugin, manager, provider


@pytest.mark.asyncio
async def test_end_to_end_background_compression_merges_messages_arriving_in_flight():
    snapshot = make_history(rounds=4, size=1000)
    provider = FakeProvider(wait_for_release=True)
    plugin, manager, _ = make_plugin(history=snapshot, provider=provider)

    task = asyncio.create_task(
        plugin._run_background_compression(
            "umo:test",
            manager.cid,
            copy.deepcopy(snapshot),
            provider,
        )
    )

    await asyncio.wait_for(provider.started.wait(), timeout=2)
    extra = [
        _dump("user", "message-arrived-during-compression"),
        _dump("assistant", "reply-arrived-during-compression"),
    ]
    manager.append(*extra)
    provider.release.set()

    applied = await asyncio.wait_for(task, timeout=2)

    assert applied is True
    assert manager.update_calls == 1
    assert manager.history[-2:] == extra
    assert manager.history != snapshot + extra
    assert any(
        isinstance(item.get("content"), str)
        and item["content"].startswith("Our previous history conversation summary:")
        for item in manager.history
    )


@pytest.mark.asyncio
async def test_end_to_end_stale_snapshot_is_discarded_without_writeback():
    snapshot = make_history(rounds=4, size=1000)
    provider = FakeProvider(wait_for_release=True)
    plugin, manager, _ = make_plugin(history=snapshot, provider=provider)

    task = asyncio.create_task(
        plugin._run_background_compression(
            "umo:stale",
            manager.cid,
            copy.deepcopy(snapshot),
            provider,
        )
    )

    await asyncio.wait_for(provider.started.wait(), timeout=2)
    manager.history[0] = _dump("user", "history-was-replaced-by-another-compressor")
    stale_history = copy.deepcopy(manager.history)
    provider.release.set()

    applied = await asyncio.wait_for(task, timeout=2)

    assert applied is False
    assert manager.update_calls == 0
    assert manager.history == stale_history


def test_multimodal_token_estimation_does_not_count_data_url_as_plain_text():
    huge_data_url = "data:image/png;base64," + ("A" * 200_000)
    history = [
        _dump(
            "user",
            [
                TextPart(text="inspect this image"),
                ImageURLPart(
                    image_url=ImageURLPart.ImageURL(url=huge_data_url)
                ),
            ],
        ),
        _dump("assistant", "done"),
    ]
    plugin, _, _ = make_plugin(history=history)

    tokens, mode = plugin._estimate_tokens(history)

    assert mode == "multimodal"
    assert 700 <= tokens < 5_000


@pytest.mark.asyncio
async def test_atomic_replace_merges_exact_delta():
    snapshot = make_history(rounds=2, size=50)
    plugin, manager, _ = make_plugin(history=snapshot)
    extra = [
        _dump("user", "new-user"),
        _dump("assistant", "new-assistant"),
    ]
    manager.append(*extra)
    compressed = [
        _dump("user", "Our previous history conversation summary: compact"),
        _dump("assistant", "Acknowledged the summary of our previous conversation history."),
    ]

    applied = await plugin._atomic_replace(
        "umo:delta",
        manager.cid,
        snapshot,
        compressed,
    )

    assert applied is True
    assert manager.history == compressed + extra


@pytest.mark.asyncio
async def test_only_one_background_lifecycle_task_per_umo(monkeypatch):
    plugin, _, _ = make_plugin()
    blocker = asyncio.Event()
    starts = 0

    async def blocked_check(umo: str) -> None:
        nonlocal starts
        starts += 1
        await blocker.wait()

    monkeypatch.setattr(plugin, "_background_check", blocked_check)
    event = SimpleNamespace(unified_msg_origin="umo:unique")

    await plugin.maybe_compress(event, None)
    await asyncio.sleep(0)
    await plugin.maybe_compress(event, None)
    await asyncio.sleep(0)

    assert starts == 1
    assert len(plugin._background_tasks) == 1

    await plugin.terminate()
    assert plugin._background_tasks == {}


@pytest.mark.asyncio
async def test_failed_attempt_uses_retry_cooldown_not_success_cooldown(monkeypatch):
    plugin, _, _ = make_plugin(
        config={
            "retry_cooldown_seconds": 30,
            "cooldown_seconds": 300,
        }
    )
    run = AsyncMock(return_value=False)
    monkeypatch.setattr(plugin, "_run_background_compression", run)
    monkeypatch.setattr(plugin, "_estimate_tokens", lambda history: (100, "text"))
    plugin.context.provider.provider_config["max_context_tokens"] = 100

    now = [100.0]
    monkeypatch.setattr(plugin_module.time, "monotonic", lambda: now[0])

    await plugin._maybe_run_compression("umo:retry")
    assert run.await_count == 1
    assert "umo:retry" in plugin._last_attempt
    assert "umo:retry" not in plugin._last_success

    now[0] = 120.0
    await plugin._maybe_run_compression("umo:retry")
    assert run.await_count == 1

    now[0] = 131.0
    await plugin._maybe_run_compression("umo:retry")
    assert run.await_count == 2


@pytest.mark.asyncio
async def test_success_uses_full_success_cooldown(monkeypatch):
    plugin, _, _ = make_plugin(
        config={
            "retry_cooldown_seconds": 30,
            "cooldown_seconds": 300,
        }
    )
    run = AsyncMock(return_value=True)
    monkeypatch.setattr(plugin, "_run_background_compression", run)
    monkeypatch.setattr(plugin, "_estimate_tokens", lambda history: (100, "text"))
    plugin.context.provider.provider_config["max_context_tokens"] = 100

    now = [100.0]
    monkeypatch.setattr(plugin_module.time, "monotonic", lambda: now[0])

    await plugin._maybe_run_compression("umo:success")
    assert run.await_count == 1
    assert plugin._last_success["umo:success"] == 100.0

    now[0] = 131.0
    await plugin._maybe_run_compression("umo:success")
    assert run.await_count == 1

    now[0] = 401.0
    await plugin._maybe_run_compression("umo:success")
    assert run.await_count == 2


def test_config_clamps_and_invalid_regex_are_safe():
    plugin, _, _ = make_plugin(
        config={
            "trigger_ratio": 999,
            "keep_recent_ratio": -5,
            "nl_patterns": ["(", r"压缩.*上下文"],
        }
    )

    assert plugin._trigger_ratio() == 0.95
    assert plugin._keep_recent_ratio() == 0.0
    assert plugin._matches_nl_pattern("普通消息") is False
    assert plugin._matches_nl_pattern("压缩一下上下文") is True
