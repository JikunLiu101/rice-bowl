"""rice-bowl: the persona can see how much "rice" (DeepSeek balance) it has left, what it eats a day, and how long
it lasts.

- `rice_check` tool, only in the owner's private chat (it is the owner's money): balance now, eaten today and the
  last few days, average per day, days left, last top-up, and the tokens of the recent chats (from AstrBot's stats).
- Every `check_hours` the balance is recorded; the spending and top-ups come from the differences.
- When the rice is running low, a one-line note goes with the owner's message, at most once a day.
- Works with memory-core (who the owner is, what they are called); without it, `owner_ids` and "主人".
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import aiohttp

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart

from .bowl import BalanceError, Ledger, low_note, panel_data, parse_balance, report, report_text, strip_blocks
from .panel import Panel

PLUGIN_NAME = "astrbot_plugin_rice_bowl"
MEMORY_CORE = "astrbot_plugin_memory_core"
BALANCE_URL = "https://api.deepseek.com/user/balance"


def _now() -> datetime:
    return datetime.now().astimezone()


class RiceBowl(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.cfg = config or {}
        self.path = Path(star.StarTools.get_data_dir(PLUGIN_NAME)) / "ledger.json"
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.last_error = ""
        try:
            Panel(self).register()
        except Exception as exc:  # noqa: BLE001 - the dashboard is optional
            logger.warning(f"[rice-bowl] dashboard not registered: {exc}")

    async def initialize(self) -> None:
        if self.cfg.get("enabled", True):
            self._task = asyncio.create_task(self._loop())

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()

    # ---------------------------------------------------------------- who is the owner

    def _mem(self):
        try:
            meta = self.context.get_registered_star(MEMORY_CORE)
        except Exception:  # noqa: BLE001
            return None
        plugin = getattr(meta, "star_cls", None) if meta and getattr(meta, "activated", True) else None
        return plugin if getattr(plugin, "core", None) is not None else None

    def _owner_ids(self) -> set[str]:
        mem = self._mem()
        ids = getattr(mem, "owner_ids", None) if mem else None
        return {str(x) for x in (ids or self.cfg.get("owner_ids", []) or []) if str(x).strip()}

    def _owner_title(self) -> str:
        mem = self._mem()
        return (getattr(mem, "owner_title", "") if mem else "") or "主人"

    def _owner_private(self, event: AstrMessageEvent) -> bool:
        return not event.get_group_id() and str(event.get_sender_id()) in self._owner_ids()

    # ---------------------------------------------------------------- reading the balance

    def _key(self) -> str:
        """The plugin's own key, else the key of AstrBot's DeepSeek provider. Never logged."""
        own = str(self.cfg.get("deepseek_api_key") or "").strip()
        if own:
            return own
        try:
            sources = self.context.get_config().get("provider_sources", []) or []
        except Exception:  # noqa: BLE001
            sources = []
        for src in sources:
            if "api.deepseek.com" in str(src.get("api_base") or "") and src.get("enable", True):
                keys = src.get("key") or []
                keys = [keys] if isinstance(keys, str) else keys
                for k in keys:
                    if str(k).strip():
                        return str(k).strip()
        return ""

    async def fetch(self):
        """Read the balance now and record it. Raises BalanceError with a reason she can tell the owner."""
        owner = self._owner_title()
        key = self._key()
        if not key:
            raise BalanceError(f"没找到 DeepSeek 的 key，要{owner}在 AstrBot 里配好 DeepSeek，或者填进这个插件的设置")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.get(BALANCE_URL, headers={"Authorization": f"Bearer {key}",
                                                       "Accept": "application/json"}) as resp:
                    if resp.status == 401:
                        raise BalanceError(f"DeepSeek 不认这个 key 了，要{owner}检查一下 key")
                    if resp.status >= 400:
                        raise BalanceError(f"DeepSeek 那边出错了（{resp.status}），过一会儿再看")
                    data = await resp.json(content_type=None)
        except BalanceError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise BalanceError(f"连不上 DeepSeek（{type(exc).__name__}），可能是网络不好") from exc
        snap = parse_balance(data, _now())
        if snap is None:
            raise BalanceError("DeepSeek 没告诉我余额，过一会儿再看")
        async with self._lock:
            ledger = await asyncio.to_thread(Ledger.load, self.path)
            ledger.add(snap, merge_minutes=float(self.cfg.get("merge_minutes", 30)))
            await asyncio.to_thread(ledger.save, self.path)
        self.last_error = ""
        return snap

    async def _loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                snap = await self.fetch()
                logger.info(f"[rice-bowl] balance recorded ({snap.currency})")
            except BalanceError as exc:
                self.last_error = str(exc)
                logger.warning(f"[rice-bowl] could not read the balance: {exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[rice-bowl] balance check failed: {exc}")
            await asyncio.sleep(max(0.5, float(self.cfg.get("check_hours", 3))) * 3600)

    def _report(self):
        return report(Ledger.load(self.path).items(), _now(), days=int(self.cfg.get("trend_days", 7)))

    def panel_data(self) -> dict:
        """What the dashboard shows (see bowl.panel_data)."""
        return panel_data(Ledger.load(self.path).items(), _now(), trend_days=int(self.cfg.get("trend_days", 7)),
                          token_days=self._token_days(14), warn_days=float(self.cfg.get("warn_days", 5)),
                          warn_balance=float(self.cfg.get("warn_balance", 3)))

    # ---------------------------------------------------------------- tokens of the recent chats (AstrBot's stats)

    def _token_days(self, days: int = 4) -> list[tuple[date, int, int]]:
        """(day, input tokens, cached) of the chat replies, from AstrBot's provider_stats. Background calls made
        with llm_generate (consolidation, diary, outings) are not in there."""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path  # noqa: PLC0415

            db = Path(get_astrbot_data_path()) / "data_v4.db"
            if not db.exists():
                return []
            start = (_now() - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
            with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5) as c:
                rows = c.execute("SELECT start_time, token_input_other, token_input_cached FROM provider_stats"
                                 " WHERE start_time >= ?", (start.timestamp(),)).fetchall()
        except Exception as exc:  # noqa: BLE001 - only extra detail
            logger.debug(f"[rice-bowl] token stats unavailable: {exc}")
            return []
        per: dict[date, list[int]] = {}
        for ts, other, cached in rows:
            d = datetime.fromtimestamp(ts).date()
            acc = per.setdefault(d, [0, 0])
            acc[0] += int(other or 0) + int(cached or 0)
            acc[1] += int(cached or 0)
        return [(d, v[0], v[1]) for d, v in sorted(per.items())]

    # ---------------------------------------------------------------- what she can do

    def being_abilities(self, owner: str) -> list[tuple[str, str, str]]:
        if not self.cfg.get("enabled", True):
            return []
        text = (f"和{owner}私聊时用 rice_check 看自己的“饭”（DeepSeek 余额）还剩多少、今天吃了多少、最近每天吃多少、"
                f"还能吃几天、{owner}上次什么时候给你充的；{owner}说起大白饭、吃饭、充 token、余额，就是在说它")
        if not self._key():
            text += f"。现在看不了：没找到 DeepSeek 的 key，要{owner}在 AstrBot 里配好"
        return [("rice-bowl:check", "看自己还剩多少饭", text)]

    # ---------------------------------------------------------------- the tool

    @filter.llm_tool(name="rice_check")
    async def tool_rice_check(self, event: AstrMessageEvent):
        """See how much "rice" you have left: your DeepSeek balance (what keeps you thinking and talking), what you
        ate today and the last few days, how long it will last, and when you were last topped up. Use it when
        the person talks about your rice / food / eating (大白饭, 吃饭, 饭够不够), topping up tokens (充 token),
        or your balance. Only in a private chat with the person closest to you."""
        owner = self._owner_title()
        if not self._owner_private(event):
            logger.info("[rice-bowl] rice_check refused outside the owner's private chat")
            return f"这是{owner}的钱包，只有和{owner}私聊时才能看"
        problem = ""
        try:
            await self.fetch()
        except BalanceError as exc:
            problem = str(exc)
            self.last_error = problem
        r = await asyncio.to_thread(self._report)
        if r is None:
            return f"现在看不到饭量：{problem or '还没记下余额'}。看不到的话，跟{owner}说一声"
        text = report_text(r, owner, await asyncio.to_thread(self._token_days))
        if problem:
            text = f"（刚才没查到最新的：{problem}。下面是上次记下的）\n" + text
        return text

    # ---------------------------------------------------------------- low rice note

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest):
        """In the owner's private chat, once a day when the rice is running low: a note with their message (not in
        the system prompt, so the prompt cache is kept; taken out of the earlier messages in the history)."""
        try:
            strip_blocks(req.contexts, ("rice_bowl",))
        except Exception as exc:  # noqa: BLE001 - never block a reply
            logger.warning(f"[rice-bowl] history cleanup failed: {exc}")
        if not self.cfg.get("enabled", True) or not self._owner_private(event):
            return

        def note() -> str:
            ledger = Ledger.load(self.path)
            today = _now().date().isoformat()
            if ledger.warned_on == today:
                return ""
            text = low_note(report(ledger.items(), _now(), days=int(self.cfg.get("trend_days", 7))),
                            self._owner_title(), warn_days=float(self.cfg.get("warn_days", 5)),
                            warn_balance=float(self.cfg.get("warn_balance", 3)))
            if text:
                ledger.warned_on = today
                ledger.save(self.path)
            return text
        async with self._lock:
            text = await asyncio.to_thread(note)
        if text:
            req.extra_user_content_parts.append(TextPart(text=text))
            logger.info("[rice-bowl] told her the rice is running low")
