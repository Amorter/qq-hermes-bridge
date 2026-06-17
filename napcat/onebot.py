"""OneBot v11 forward-WebSocket client for NapCat.

NapCat runs a *forward* WebSocket **server** (``正向 WebSocket``); this client
dials out to it, receives message/notice events, and issues API actions
(``send_group_msg``, ``upload_group_file`` …) over the same socket using the
OneBot ``echo`` correlation convention.

The client owns a supervised connect loop: it reconnects with exponential
backoff when the socket drops, and resolves in-flight API calls' futures from
the single receive loop.  Transport only — all OneBot semantics (segment
parsing, MessageEvent translation) live in ``adapter.py``.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Async callback invoked once per inbound event (post_type present).
EventCallback = Callable[[Dict[str, Any]], Awaitable[None]]


class OneBotError(Exception):
    """An OneBot API action returned a non-zero ``retcode``."""

    def __init__(self, action: str, retcode: Any, message: str = "") -> None:
        self.action = action
        self.retcode = retcode
        self.message = message
        super().__init__(f"OneBot API '{action}' failed (retcode={retcode}): {message}")


class OneBotClient:
    """Minimal supervised OneBot v11 forward-WS client.

    Args:
        ws_url: NapCat forward-WS URL, e.g. ``ws://127.0.0.1:3001``.
        access_token: Optional OneBot access token (sent as ``?access_token=``).
        on_event: Async callback invoked for each inbound event frame.
        api_timeout: Seconds to wait for an API action's response.
    """

    def __init__(
        self,
        ws_url: str,
        access_token: str = "",
        *,
        on_event: EventCallback,
        api_timeout: float = 30.0,
    ) -> None:
        self._ws_url = ws_url
        self._access_token = access_token
        self._on_event = on_event
        self._api_timeout = api_timeout

        self._session: Any = None  # aiohttp.ClientSession
        self._ws: Any = None  # aiohttp.ClientWebSocketResponse
        self._supervisor: Optional[asyncio.Task] = None
        self._running = False
        self._connected = asyncio.Event()

        # echo -> Future for pending API calls
        self._pending: Dict[str, "asyncio.Future[Dict[str, Any]]"] = {}
        self._echo_seq = itertools.count(1)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Spawn the supervised connect/receive loop (returns immediately)."""
        if self._supervisor is not None:
            return
        self._running = True
        self._supervisor = asyncio.create_task(self._run_supervised())

    async def stop(self) -> None:
        """Stop the loop, close the socket, and fail any pending API calls."""
        self._running = False
        self._connected.clear()
        if self._supervisor and not self._supervisor.done():
            self._supervisor.cancel()
            try:
                await self._supervisor
            except asyncio.CancelledError:
                pass
        self._supervisor = None
        await self._close_socket()
        self._fail_pending(ConnectionError("client stopped"))

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        """Block until the socket is connected (or *timeout* elapses)."""
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ── API calls ─────────────────────────────────────────────────────────

    async def call_api(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Invoke an OneBot action and return its ``data`` payload.

        Raises ``ConnectionError`` if not connected, ``OneBotError`` on a
        non-zero retcode, and ``asyncio.TimeoutError`` if no response arrives.
        """
        if not self.connected or self._ws is None:
            raise ConnectionError(f"OneBot not connected; cannot call '{action}'")

        echo = str(next(self._echo_seq))
        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[Dict[str, Any]]" = loop.create_future()
        self._pending[echo] = fut
        try:
            await self._ws.send_str(json.dumps({"action": action, "params": params or {}, "echo": echo}))
            resp = await asyncio.wait_for(fut, timeout=timeout or self._api_timeout)
        finally:
            self._pending.pop(echo, None)

        if resp.get("retcode") not in (0, 1):
            # retcode 1 = async accepted; treat anything else as failure.
            raise OneBotError(action, resp.get("retcode"), resp.get("msg") or resp.get("wording") or "")
        return resp.get("data") or {}

    # ── Internals ─────────────────────────────────────────────────────────

    async def _run_supervised(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                await self._connect_once()
                backoff = 1.0  # reset after a clean session
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — supervisor must not die
                logger.warning("OneBot connection error: %s", exc)
            finally:
                self._connected.clear()
                await self._close_socket()
                self._fail_pending(ConnectionError("socket closed"))
            if not self._running:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _connect_once(self) -> None:
        import aiohttp

        url = self._ws_url
        if self._access_token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}access_token={self._access_token}"

        self._session = aiohttp.ClientSession()
        logger.info("OneBot: connecting to %s", self._ws_url)
        self._ws = await self._session.ws_connect(url, heartbeat=30.0, max_msg_size=0)
        self._connected.set()
        logger.info("OneBot: connected")

        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                self._on_frame(msg.data)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break

    def _on_frame(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return

        # API response: correlate by echo.
        echo = data.get("echo")
        if echo is not None:
            fut = self._pending.get(str(echo))
            if fut and not fut.done():
                fut.set_result(data)
            return

        # Inbound event: dispatch (fire-and-forget so a slow handler doesn't
        # stall the receive loop / heartbeat).
        if data.get("post_type"):
            asyncio.create_task(self._dispatch_event(data))

    async def _dispatch_event(self, data: Dict[str, Any]) -> None:
        try:
            await self._on_event(data)
        except Exception:  # noqa: BLE001 — one bad event must not kill the loop
            logger.exception("OneBot: event handler raised")

    async def _close_socket(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:  # noqa: BLE001
                pass
            self._session = None

    def _fail_pending(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()


# ── OneBot v11 message-segment helpers ────────────────────────────────────
#
# A message is a list of segments: ``{"type": str, "data": dict}``.

def seg_text(text: str) -> Dict[str, Any]:
    return {"type": "text", "data": {"text": text}}


def seg_at(qq: str) -> Dict[str, Any]:
    return {"type": "at", "data": {"qq": str(qq)}}


def seg_reply(message_id: str) -> Dict[str, Any]:
    return {"type": "reply", "data": {"id": str(message_id)}}


def seg_image(file: str) -> Dict[str, Any]:
    """Build an image segment. *file* may be ``base64://…``, ``file://…`` or an URL."""
    return {"type": "image", "data": {"file": file}}


def _as_segments(message: Any) -> List[Dict[str, Any]]:
    """Normalize an OneBot ``message`` field (array or CQ-string) to a list."""
    if isinstance(message, list):
        return [s for s in message if isinstance(s, dict)]
    # String-format messages are uncommon with NapCat (array is the default),
    # so treat a bare string as a single text segment.
    if isinstance(message, str) and message:
        return [seg_text(message)]
    return []


def extract_text(message: Any) -> str:
    """Concatenate the text of all ``text`` segments."""
    parts = [s.get("data", {}).get("text", "") for s in _as_segments(message) if s.get("type") == "text"]
    return "".join(parts).strip()


def iter_images(message: Any) -> List[Dict[str, Any]]:
    """Return the ``data`` dict of every ``image`` segment (has ``url``/``file``)."""
    return [s.get("data", {}) for s in _as_segments(message) if s.get("type") == "image"]


def find_reply_id(message: Any) -> Optional[str]:
    """Return the replied-to message id from a ``reply`` segment, if present."""
    for s in _as_segments(message):
        if s.get("type") == "reply":
            rid = s.get("data", {}).get("id")
            if rid is not None:
                return str(rid)
    return None


def has_at(message: Any, qq: str) -> bool:
    """True if the message @-mentions *qq* (or @all)."""
    qq = str(qq)
    for s in _as_segments(message):
        if s.get("type") == "at":
            target = str(s.get("data", {}).get("qq", ""))
            if target == qq or target == "all":
                return True
    return False
