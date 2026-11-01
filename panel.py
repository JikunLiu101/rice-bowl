"""The owner's dashboard for rice-bowl: a plugin view (pages/rice/) and the Web API behind it.

AstrBot serves plugin Web APIs only to a logged-in dashboard user (or an API key with the plugin scope). The only
action is "check the balance now", the same request the plugin makes every few hours; the key never leaves here.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .bowl import BalanceError

PREFIX = "/astrbot_plugin_rice_bowl/page"


def _ok(data: Any) -> dict:
    return {"status": "ok", "data": data}


def _error(message: str) -> dict:
    return {"status": "error", "message": message}


class Panel:
    def __init__(self, plugin) -> None:
        self.plugin = plugin

    def register(self) -> bool:
        register = getattr(self.plugin.context, "register_web_api", None)
        if not callable(register):
            return False
        for name, handler, methods in [("overview", self.overview, ["GET"]), ("check", self.check, ["POST"])]:
            register(f"{PREFIX}/{name}", self._guarded(handler), methods, f"rice-bowl dashboard: {name}")
        return True

    @staticmethod
    def _guarded(handler):
        async def run(**_):
            try:
                return _ok(await handler())
            except Exception as exc:  # noqa: BLE001 - the page shows the message instead of a blank screen
                from astrbot.api import logger  # noqa: PLC0415

                logger.warning(f"[rice-bowl] dashboard {handler.__name__} failed: {exc}")
                return _error(f"{type(exc).__name__}: {exc}")

        run.__name__ = handler.__name__
        return run

    async def overview(self) -> dict:
        p = self.plugin
        data = await asyncio.to_thread(p.panel_data)
        data.update(owner=p._owner_title(), enabled=bool(p.cfg.get("enabled", True)), key_ok=bool(p._key()),
                    last_error=p.last_error, check_hours=float(p.cfg.get("check_hours", 3)))
        return data

    async def check(self) -> dict:
        problem = ""
        try:
            await self.plugin.fetch()
        except BalanceError as exc:
            problem = str(exc)
            self.plugin.last_error = problem
        data = await self.overview()
        data["checked"] = not problem
        data["problem"] = problem
        return data
