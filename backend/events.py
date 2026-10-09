"""Thread-safe, non-blocking in-process event bus for SSE consumers."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from .db import now_iso

Event = dict[str, Any]

_lock = threading.Lock()
_subscribers: dict[asyncio.Queue[Event], asyncio.AbstractEventLoop] = {}


def subscribe() -> asyncio.Queue[Event]:
    """Register a bounded event queue on the caller's running event loop."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)
    with _lock:
        _subscribers[queue] = loop
    return queue


def unsubscribe(queue: asyncio.Queue[Event]) -> None:
    """Remove a queue from the subscriber set."""
    with _lock:
        _subscribers.pop(queue, None)


def _put_nowait(queue: asyncio.Queue[Event], event: Event) -> None:
    """Enqueue without blocking; discard the oldest item if the queue is full."""
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # A concurrent consumer/publisher can fill it again. Dropping this
            # event is preferable to blocking the agent or API request.
            pass


def publish(event_type: str, data: dict[str, Any]) -> None:
    """Publish an event to subscribers without waiting for their consumers."""
    event: Event = {"type": event_type, "data": data, "ts": now_iso()}

    with _lock:
        subscribers = tuple(_subscribers.items())

    stale: list[asyncio.Queue[Event]] = []
    for queue, loop in subscribers:
        if loop.is_closed():
            stale.append(queue)
            continue
        try:
            loop.call_soon_threadsafe(_put_nowait, queue, event)
        except RuntimeError:
            # The loop may have closed between is_closed() and scheduling.
            stale.append(queue)

    if stale:
        with _lock:
            for queue in stale:
                _subscribers.pop(queue, None)