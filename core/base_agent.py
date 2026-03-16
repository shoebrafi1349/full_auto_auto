"""
core/base_agent.py — Abstract base for all agents
===================================================
Provides logging, message sending helpers, and the
abstract handle() method every agent must implement.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from core.messages import AgentRole, Message, MessageType, TaskStatus

if TYPE_CHECKING:
    from core.bus import AgentBus

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)


def _make_logger(role: AgentRole) -> logging.Logger:
    name = role.value
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        f"%(asctime)s  [{name.upper():12s}]  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    # Console handler
    if not logger.handlers:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        ch.setLevel(logging.INFO)
        logger.addHandler(ch)

        # Per-agent file handler
        fh = logging.FileHandler(LOGS_DIR / f"{name}.log", mode="a", encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(logging.DEBUG)
        logger.addHandler(fh)

    return logger


class BaseAgent(ABC):
    """
    All agents inherit from this.

    Subclasses implement:
        async def handle(self, message: Message) -> None

    They send replies via:
        await self.send(message)
    """

    role: AgentRole  # must be set on subclass

    def __init__(self, bus: "AgentBus"):
        self.bus = bus
        self.log = _make_logger(self.role)
        bus.subscribe(self.role, self._dispatch)
        self.log.info("Agent started")

    async def _dispatch(self, message: Message):
        """Internal: called by the bus. Wraps handle() with error catching."""
        try:
            await self.handle(message)
        except Exception as exc:
            self.log.exception("Unhandled error processing %s: %s", message, exc)
            # Escalate unexpected crashes to orchestrator
            await self.send(Message(
                type=MessageType.STATUS,
                sender=self.role,
                recipient=AgentRole.ORCHESTRATOR,
                payload={"error": str(exc), "original_message": str(message)},
                task_id=message.task_id,
                status=TaskStatus.FAILED,
            ))

    @abstractmethod
    async def handle(self, message: Message) -> None:
        """Process an incoming message. Must be implemented by every agent."""
        ...

    async def send(self, message: Message):
        """Publish a message to the bus."""
        await self.bus.publish(message)

    def info(self, msg: str, *args):
        self.log.info(msg, *args)

    def debug(self, msg: str, *args):
        self.log.debug(msg, *args)

    def warn(self, msg: str, *args):
        self.log.warning(msg, *args)

    def error(self, msg: str, *args):
        self.log.error(msg, *args)
