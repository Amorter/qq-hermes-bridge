"""Unit tests for napcat/onebot.py — pure logic, no Hermes required.

``onebot.py`` only imports stdlib at module level (aiohttp is imported lazily
inside methods), so we load it directly by path to avoid triggering the
package ``__init__`` (which imports the Hermes-dependent adapter).
"""

import asyncio
import importlib.util
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_onebot():
    path = os.path.join(_HERE, "..", "napcat", "onebot.py")
    spec = importlib.util.spec_from_file_location("napcat_onebot_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ob = _load_onebot()


# ── segment builders ──────────────────────────────────────────────────────

def test_segment_builders():
    assert ob.seg_text("hi") == {"type": "text", "data": {"text": "hi"}}
    assert ob.seg_at("123") == {"type": "at", "data": {"qq": "123"}}
    assert ob.seg_reply(99) == {"type": "reply", "data": {"id": "99"}}
    assert ob.seg_image("base64://AAA") == {"type": "image", "data": {"file": "base64://AAA"}}


# ── parsing ───────────────────────────────────────────────────────────────

def _sample_message():
    return [
        {"type": "reply", "data": {"id": "555"}},
        {"type": "at", "data": {"qq": "10001"}},
        {"type": "text", "data": {"text": " 你好 小喵 "}},
        {"type": "image", "data": {"file": "abc.jpg", "url": "https://qq/img.jpg"}},
        {"type": "text", "data": {"text": "看图"}},
    ]


def test_extract_text_concatenates_and_strips():
    assert ob.extract_text(_sample_message()) == "你好 小喵 看图"


def test_extract_text_handles_string_message():
    assert ob.extract_text("plain hello") == "plain hello"
    assert ob.extract_text(None) == ""


def test_iter_images_returns_data_dicts():
    imgs = ob.iter_images(_sample_message())
    assert len(imgs) == 1
    assert imgs[0]["url"] == "https://qq/img.jpg"
    assert ob.iter_images("plain") == []


def test_find_reply_id():
    assert ob.find_reply_id(_sample_message()) == "555"
    assert ob.find_reply_id([]) is None
    assert ob.find_reply_id([{"type": "text", "data": {"text": "x"}}]) is None


def test_has_at():
    msg = _sample_message()
    assert ob.has_at(msg, "10001") is True
    assert ob.has_at(msg, "99999") is False
    assert ob.has_at([{"type": "at", "data": {"qq": "all"}}], "10001") is True


def test_onebot_error_message():
    err = ob.OneBotError("send_group_msg", 1404, "nope")
    assert "send_group_msg" in str(err)
    assert "1404" in str(err)
    assert err.retcode == 1404


# ── client API correlation ─────────────────────────────────────────────────

class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_str(self, s):
        self.sent.append(json.loads(s))

    async def close(self):
        pass


def _make_client(ws):
    client = ob.OneBotClient("ws://x", on_event=lambda e: None)
    client._ws = ws
    client._connected.set()
    return client


def test_call_api_roundtrip():
    async def run():
        ws = _FakeWS()
        client = _make_client(ws)

        async def caller():
            return await client.call_api("send_group_msg", {"group_id": 1, "message": []}, timeout=2)

        task = asyncio.ensure_future(caller())
        await asyncio.sleep(0.01)  # let call_api send + register echo "1"
        assert ws.sent and ws.sent[0]["action"] == "send_group_msg"
        echo = ws.sent[0]["echo"]
        client._on_frame(json.dumps({"echo": echo, "retcode": 0, "data": {"message_id": 42}}))
        data = await task
        assert data == {"message_id": 42}

    asyncio.run(run())


def test_call_api_raises_on_error_retcode():
    async def run():
        ws = _FakeWS()
        client = _make_client(ws)
        task = asyncio.ensure_future(client.call_api("upload_group_file", {}, timeout=2))
        await asyncio.sleep(0.01)
        echo = ws.sent[0]["echo"]
        client._on_frame(json.dumps({"echo": echo, "retcode": 1404, "msg": "no such group"}))
        try:
            await task
            assert False, "expected OneBotError"
        except ob.OneBotError as e:
            assert e.retcode == 1404

    asyncio.run(run())


def test_call_api_not_connected():
    async def run():
        client = ob.OneBotClient("ws://x", on_event=lambda e: None)
        try:
            await client.call_api("send_group_msg", {})
            assert False, "expected ConnectionError"
        except ConnectionError:
            pass

    asyncio.run(run())


def test_call_api_accepts_status_ok_without_retcode():
    """Stream actions reply with status=ok and may omit retcode."""

    async def run():
        ws = _FakeWS()
        client = _make_client(ws)
        task = asyncio.ensure_future(client.call_api("upload_file_stream", {}, timeout=2))
        await asyncio.sleep(0.01)
        echo = ws.sent[0]["echo"]
        client._on_frame(json.dumps({"echo": echo, "status": "ok", "data": {"x": 1}}))
        assert await task == {"x": 1}

    asyncio.run(run())


def test_stream_upload_protocol():
    async def run():
        ws = _FakeWS()
        client = _make_client(ws)
        data = b"x" * (300 * 1024)  # 300KB → 2 chunks at 256KB

        async def feeder():
            seen = 0
            while True:
                await asyncio.sleep(0.005)
                while seen < len(ws.sent):
                    msg = ws.sent[seen]
                    seen += 1
                    echo = msg["echo"]
                    params = msg["params"]
                    if params.get("is_complete"):
                        client._on_frame(json.dumps({
                            "echo": echo, "status": "ok", "retcode": 0,
                            "data": {"type": "response", "status": "file_complete",
                                     "file_path": "/napcat/tmp/foo.bin",
                                     "file_size": len(data), "sha256": "deadbeef"},
                        }))
                        return
                    client._on_frame(json.dumps({
                        "echo": echo, "status": "ok", "retcode": 0,
                        "data": {"type": "stream", "status": "chunk_received",
                                 "received_chunks": params["chunk_index"] + 1,
                                 "total_chunks": params["total_chunks"]},
                    }))

        feeder_task = asyncio.ensure_future(feeder())
        path = await client.stream_upload(data, "foo.bin", chunk_size=256 * 1024)
        await feeder_task

        assert path == "/napcat/tmp/foo.bin"
        chunk_calls = [m for m in ws.sent if "chunk_data" in m["params"]]
        complete_calls = [m for m in ws.sent if m["params"].get("is_complete")]
        assert len(chunk_calls) == 2
        assert len(complete_calls) == 1
        first = chunk_calls[0]["params"]
        assert first["total_chunks"] == 2
        assert first["file_size"] == len(data)
        assert first["chunk_index"] == 0
        assert len(first["expected_sha256"]) == 64  # sha256 hex
        assert first["filename"] == "foo.bin"

    asyncio.run(run())


def test_stream_upload_raises_without_file_path():
    async def run():
        ws = _FakeWS()
        client = _make_client(ws)

        async def feeder():
            seen = 0
            while True:
                await asyncio.sleep(0.005)
                while seen < len(ws.sent):
                    msg = ws.sent[seen]
                    seen += 1
                    echo = msg["echo"]
                    if msg["params"].get("is_complete"):
                        # completion without file_path → should raise
                        client._on_frame(json.dumps({
                            "echo": echo, "status": "ok", "retcode": 0,
                            "data": {"type": "response", "status": "file_complete"},
                        }))
                        return
                    client._on_frame(json.dumps({"echo": echo, "status": "ok", "retcode": 0, "data": {}}))

        feeder_task = asyncio.ensure_future(feeder())
        try:
            await client.stream_upload(b"hello", "h.txt", chunk_size=256 * 1024)
            assert False, "expected OneBotError"
        except ob.OneBotError:
            pass
        finally:
            await feeder_task

    asyncio.run(run())
