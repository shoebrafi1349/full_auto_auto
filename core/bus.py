"""
core/bus.py — Async message bus
================================
Central hub for all inter-agent communication.
Agents publish messages; subscribed agents receive them via async queues.
Supports direct routing and broadcast.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Callable, Coroutine

from core.messages import AgentRole, Message, MessageType

log = logging.getLogger("bus")


class AgentBus:
    """
    Async pub/sub message bus.

    Usage:
        bus = AgentBus()
        bus.subscribe(AgentRole.TESTER, handler_coroutine)
        await bus.publish(message)
    """

    def __init__(self):
        # role → list of async handlers
        self._handlers: dict[AgentRole, list[Callable]] = defaultdict(list)
        # Full message history for debugging / supervisor review
        self._history: list[Message] = []
        self._lock = asyncio.Lock()

    def subscribe(self, role: AgentRole, handler: Callable[..., Coroutine]):
        """Register an async handler for messages addressed to `role`."""
        self._handlers[role].append(handler)
        log.debug("subscribed %s", role.value)

    async def publish(self, message: Message):
        """
        Deliver message to all handlers registered for message.recipient.
        Fires all handlers concurrently.
        """
        async with self._lock:
            self._history.append(message)

        log.info("BUS  %s", message)

        handlers = self._handlers.get(message.recipient, [])
        if not handlers:
            log.warning("No handlers for %s — message dropped", message.recipient.value)
            return

        await asyncio.gather(*(h(message) for h in handlers))

    async def broadcast(self, message: Message, roles: list[AgentRole]):
        """Send the same message to multiple roles simultaneously."""
        tasks = []
        for role in roles:
            m = Message(
                type=message.type,
                sender=message.sender,
                recipient=role,
                payload=message.payload,
                task_id=message.task_id,
                attempt=message.attempt,
                status=message.status,
            )
            tasks.append(self.publish(m))
        await asyncio.gather(*tasks)

    def history(self, task_id: str = "") -> list[Message]:
        """Return message history, optionally filtered by task_id."""
        if task_id:
            return [m for m in self._history if m.task_id == task_id]
        return list(self._history)

    def last_n(self, n: int) -> list[Message]:
        return self._history[-n:]
