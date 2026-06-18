"""Tests for napcat/adapter.py.

These exercise the adapter against the *real* Hermes base classes, so they run
only where Hermes is importable (e.g. the deployment box) and are skipped
otherwise.
"""

import asyncio
import os

import pytest

pytest.importorskip("gateway.platforms.base")

from napcat import adapter as ad  # noqa: E402


@pytest.fixture(autouse=True)
def _register_platform():
    """Register 'napcat' so ``Platform('napcat')`` resolves during construction."""
    from gateway.platform_registry import platform_registry, PlatformEntry

    if not platform_registry.is_registered("napcat"):
        platform_registry.register(
            PlatformEntry(
                name="napcat",
                label="NapCat",
                adapter_factory=lambda c: None,
                check_fn=lambda: True,
            )
        )


def _make_adapter(**extra):
    base = {"ws_url": "ws://x", "bot_qq": "10000", "require_mention": True}
    base.update(extra)

    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.extra = base
    return ad.NapCatAdapter(cfg)


# ── pure helpers ────────────────────────────────────────────────────────────

def test_route():
    assert ad.NapCatAdapter._route("group:123") == ("group", 123)
    assert ad.NapCatAdapter._route("user:456") == ("private", 456)
    assert ad.NapCatAdapter._route("789") == ("group", 789)


def test_to_onebot_file_passthrough():
    adapter = _make_adapter()
    assert adapter._to_onebot_file("base64://AAA") == "base64://AAA"
    assert adapter._to_onebot_file("https://x/y.png") == "https://x/y.png"
    assert adapter._to_onebot_file("http://x/y.png") == "http://x/y.png"


# ── module-level config functions ───────────────────────────────────────────

def test_configured(monkeypatch):
    monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
    assert ad.is_connected(None) is False
    assert ad.validate_config(None) is False
    monkeypatch.setenv("ONEBOT_WS_URL", "ws://x")
    assert ad.is_connected(None) is True


def test_env_enablement(monkeypatch):
    monkeypatch.delenv("ONEBOT_WS_URL", raising=False)
    assert ad._env_enablement() is None
    monkeypatch.setenv("ONEBOT_WS_URL", "ws://x")
    monkeypatch.setenv("BOT_QQ", "42")
    seeded = ad._env_enablement()
    assert seeded["ws_url"] == "ws://x"
    assert seeded["bot_qq"] == "42"


def test_check_requirements():
    # aiohttp ships with Hermes, so this is True wherever these tests run.
    assert ad.check_requirements() is True


# ── registration wiring ──────────────────────────────────────────────────────

def test_register_wires_expected_entry():
    class _Ctx:
        def __init__(self):
            self.kw = None

        def register_platform(self, **kw):
            self.kw = kw

    ctx = _Ctx()
    ad.register(ctx)
    assert ctx.kw["name"] == "napcat"
    assert ctx.kw["allowed_users_env"] == "NAPCAT_ALLOWED_USERS"
    assert ctx.kw["allow_all_env"] == "NAPCAT_ALLOW_ALL_USERS"
    assert "ONEBOT_WS_URL" in ctx.kw["required_env"]
    assert callable(ctx.kw["adapter_factory"])
    assert callable(ctx.kw["check_fn"])
    assert "QQ" in ctx.kw["platform_hint"]


# ── inbound translation ──────────────────────────────────────────────────────

def test_to_message_event_group_text():
    adapter = _make_adapter()
    data = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 777,
        "user_id": "10001",
        "message": [{"type": "text", "data": {"text": "hello"}}],
        "sender": {"nickname": "Bob"},
        "message_id": "5",
    }
    event = asyncio.run(adapter._to_message_event(data, "hello", True))
    assert event.text == "hello"
    assert event.source.chat_id == "group:777"
    assert event.source.user_id == "10001"
    assert event.source.user_name == "Bob"
    assert event.source.chat_type == "group"
    assert event.message_id == "5"


def test_to_message_event_private_returns_none_when_empty():
    adapter = _make_adapter()
    data = {
        "post_type": "message",
        "message_type": "private",
        "user_id": "10001",
        "message": [],
        "sender": {},
        "message_id": "6",
    }
    assert asyncio.run(adapter._to_message_event(data, "", False)) is None


def test_on_event_group_mention_gating():
    adapter = _make_adapter()
    seen = []

    async def fake_handle(ev):
        seen.append(ev)

    adapter.handle_message = fake_handle
    adapter._message_handler = lambda e: None  # truthy

    base = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 777,
        "user_id": "10001",
        "sender": {"nickname": "Bob"},
        "message_id": "5",
    }

    # No @-mention, not a command → gated out.
    no_mention = dict(base, message=[{"type": "text", "data": {"text": "no mention"}}])
    asyncio.run(adapter._on_event(no_mention))
    assert seen == []

    # @bot present → dispatched.
    mention = dict(
        base,
        message=[{"type": "at", "data": {"qq": "10000"}}, {"type": "text", "data": {"text": "hi"}}],
    )
    asyncio.run(adapter._on_event(mention))
    assert len(seen) == 1
    assert seen[0].text == "hi"


def test_on_event_filters_self_messages():
    adapter = _make_adapter()
    seen = []

    async def fake_handle(ev):
        seen.append(ev)

    adapter.handle_message = fake_handle
    adapter._message_handler = lambda e: None

    self_msg = {
        "post_type": "message",
        "message_type": "private",
        "user_id": "10000",  # == bot_qq
        "message": [{"type": "text", "data": {"text": "echo"}}],
        "sender": {},
        "message_id": "7",
    }
    asyncio.run(adapter._on_event(self_msg))
    assert seen == []
