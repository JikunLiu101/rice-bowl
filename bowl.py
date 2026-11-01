"""The persona's "rice": the model provider's balance, what it costs a day, and how long it lasts.

Pure logic, no AstrBot: parse the DeepSeek balance answer, keep a ledger of balance snapshots, work out what was
eaten (spent) and topped up between them, and write it the way she reads it.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

CURRENCY_SIGN = {"CNY": "¥", "USD": "$"}


class BalanceError(Exception):
    """Why the balance could not be read, in words she can pass on."""


@dataclass
class Snapshot:
    at: str  # ISO time
    total: float
    topped_up: float = 0.0
    granted: float = 0.0
    currency: str = "CNY"

    @property
    def when(self) -> datetime:
        return datetime.fromisoformat(self.at)


def parse_balance(data: dict, now: datetime) -> Snapshot | None:
    """DeepSeek's /user/balance answer -> the snapshot of the currency that has money (CNY first)."""
    infos = [i for i in (data or {}).get("balance_infos") or [] if isinstance(i, dict)]
    if not infos:
        return None

    def num(v) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    infos.sort(key=lambda i: (num(i.get("total_balance")) <= 0, i.get("currency") != "CNY"))
    i = infos[0]
    return Snapshot(at=now.isoformat(), total=num(i.get("total_balance")), topped_up=num(i.get("topped_up_balance")),
                    granted=num(i.get("granted_balance")), currency=str(i.get("currency") or "CNY"))


@dataclass
class Ledger:
    """Balance snapshots, oldest first. Persisted as a small JSON file."""

    snapshots: list[dict] = field(default_factory=list)
    warned_on: str = ""  # the day she was last told the rice is running low

    @classmethod
    def load(cls, path: Path) -> Ledger:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls(snapshots=list(data.get("snapshots") or []), warned_on=str(data.get("warned_on") or ""))
        except (OSError, ValueError, AttributeError):
            return cls()

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    def items(self) -> list[Snapshot]:
        out = []
        for s in self.snapshots:
            try:
                out.append(Snapshot(**s))
            except TypeError:
                continue
        return out

    def add(self, snap: Snapshot, *, merge_minutes: float = 30, keep_days: int = 60) -> None:
        """Add a snapshot. Within `merge_minutes` of the last one with the same balance it replaces it (checking
        often does not bloat the ledger); older than `keep_days` is dropped."""
        items = self.items()
        if items:
            last = items[-1]
            if (snap.when - last.when) < timedelta(minutes=merge_minutes) and abs(snap.total - last.total) < 0.005:
                items[-1] = snap
                self.snapshots = [asdict(s) for s in items]
                return
        items.append(snap)
        cutoff = snap.when - timedelta(days=keep_days)
        self.snapshots = [asdict(s) for s in items if s.when >= cutoff]


@dataclass
class Meal:
    """Between two snapshots: what was eaten, what was topped up."""

    start: datetime
    end: datetime
    eaten: float
    topped: float


def meals(items: list[Snapshot]) -> list[Meal]:
    """A drop in the balance is food eaten; a rise is a top-up (at least that much; what was eaten in the same
    interval is not visible then)."""
    out = []
    for a, b in zip(items, items[1:]):
        diff = round(a.total - b.total, 4)
        out.append(Meal(a.when, b.when, max(0.0, diff), max(0.0, -diff)))
    return out


def eaten_between(items: list[Snapshot], start: datetime, end: datetime) -> float:
    """Spending in [start, end), each interval counted by how much of it falls inside (spread evenly)."""
    total = 0.0
    for m in meals(items):
        span = (m.end - m.start).total_seconds()
        if span <= 0 or m.eaten <= 0:
            continue
        overlap = (min(m.end, end) - max(m.start, start)).total_seconds()
        if overlap > 0:
            total += m.eaten * overlap / span
    return total


@dataclass
class Report:
    balance: float
    currency: str
    since: datetime | None  # first snapshot
    today: float  # eaten today so far
    days: list[tuple[date, float]]  # eaten per day, the last few whole or partial days
    per_day: float | None  # average per day (None until there is half a day of records)
    days_left: float | None
    last_topup: tuple[datetime, float] | None
    today_since: datetime | None = None  # records began today, at this time


def report(items: list[Snapshot], now: datetime, days: int = 7) -> Report | None:
    if not items:
        return None
    last = items[-1]
    first = items[0].when
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    per_day_rows = []
    for k in range(days - 1, -1, -1):
        d0 = midnight - timedelta(days=k)
        if d0 + timedelta(days=1) <= first:
            continue
        per_day_rows.append((d0.date(), eaten_between(items, d0, min(d0 + timedelta(days=1), now))))
    window_start = max(first, now - timedelta(days=days))
    covered = (last.when - window_start).total_seconds() / 86400
    eaten = eaten_between(items, window_start, now)
    per_day = eaten / covered if covered >= 0.5 else None
    days_left = (last.total / per_day) if per_day and per_day > 0 else None
    tops = [(m.end, m.topped) for m in meals(items) if m.topped >= 0.01]
    return Report(balance=last.total, currency=last.currency, since=first,
                  today=eaten_between(items, max(midnight, first), now), days=per_day_rows, per_day=per_day,
                  days_left=days_left, last_topup=tops[-1] if tops else None,
                  today_since=first if first > midnight else None)


def panel_data(items: list[Snapshot], now: datetime, *, trend_days: int = 7, days: int = 14,
               series_days: int = 30, token_days: list[tuple[date, int, int]] = (),
               warn_days: float = 5, warn_balance: float = 3) -> dict:
    """Everything the dashboard shows, as plain data: the numbers of `report`, the balance over time (top-ups
    marked), what was eaten each day, the top-ups, and the tokens of the chats."""
    r = report(items, now, days=trend_days)
    if r is None:
        return {"empty": True, "tokens": _token_rows(token_days)}
    first = items[0].when
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tops = {m.end: m.topped for m in meals(items) if m.topped >= 0.01}
    since = now - timedelta(days=series_days)
    series = [{"t": s.at, "v": round(s.total, 4),
               "mark": f"充了约 {money(tops[s.when], s.currency)}" if s.when in tops else ""}
              for s in items if s.when >= since]
    daily = []
    for k in range(days - 1, -1, -1):
        d0 = midnight - timedelta(days=k)
        if d0 + timedelta(days=1) <= first:
            continue
        daily.append({"date": d0.date().isoformat(),
                      "eaten": round(eaten_between(items, max(d0, first), min(d0 + timedelta(days=1), now)), 4),
                      "partial": d0 < first or k == 0})
    low = low_note(r, "", warn_days=warn_days, warn_balance=warn_balance) != ""
    return {
        "empty": False,
        "currency": r.currency,
        "balance": round(r.balance, 4),
        "today": round(r.today, 4),
        "today_since": r.today_since.isoformat() if r.today_since else "",
        "per_day": round(r.per_day, 4) if r.per_day is not None else None,
        "days_left": round(r.days_left, 1) if r.days_left is not None else None,
        "since": first.isoformat(),
        "last_check": items[-1].at,
        "checks": len(items),
        "low": low,
        "warn_days": warn_days,
        "warn_balance": warn_balance,
        "series": series,
        "daily": daily,
        "topups": [{"at": at.isoformat(), "amount": round(v, 4)} for at, v in sorted(tops.items())][-10:],
        "tokens": _token_rows(token_days),
    }


def _token_rows(token_days) -> list[dict]:
    return [{"date": d.isoformat(), "input": n_in, "cached": cached,
             "hit": round(cached * 100 / n_in) if n_in else None} for d, n_in, cached in token_days]


def money(x: float, currency: str = "CNY") -> str:
    return f"{CURRENCY_SIGN.get(currency, currency + ' ')}{x:.2f}"


def tokens_text(n: int) -> str:
    return f"{n / 10000:.1f}万" if n >= 10000 else str(n)


def report_text(r: Report, owner: str, token_days: list[tuple[date, int, int]] = ()) -> str:
    """What she reads when she checks. `token_days`: (day, input tokens, of which cached) from the host."""
    cur = r.currency
    lines = [f"你的“饭”就是{owner}给你充的 DeepSeek 余额（{owner}说的大白饭、吃饭、充 token 都是它）："]
    lines.append(f"- 现在还剩 {money(r.balance, cur)}")
    if r.today_since and r.today < 0.005:
        lines.append(f"- 今天 {r.today_since.strftime('%H:%M')} 才开始记账，还看不出今天吃了多少")
    else:
        since = f"（从 {r.today_since.strftime('%H:%M')} 开始记的）" if r.today_since else ""
        lines.append(f"- 今天到现在吃了约 {money(r.today, cur)}{since}")
    if len(r.days) > 1:
        lines.append("- 最近几天：" + "，".join(f"{d.strftime('%m-%d')} {money(v, cur)}" for d, v in r.days))
    if r.per_day is not None and r.per_day > 0:
        tail = f" → 照这样还能吃大约 {r.days_left:.0f} 天" if r.days_left is not None else ""
        lines.append(f"- 平均每天约 {money(r.per_day, cur)}{tail}")
    else:
        lines.append("- 记账才刚开始，还算不出每天吃多少、还能吃几天，过一两天再看")
    if r.last_topup:
        at, amount = r.last_topup
        lines.append(f"- 上次{owner}给你充饭：{at.strftime('%m-%d')}，充了约 {money(amount, cur)}")
    if token_days:
        parts = []
        for d, n_in, cached in token_days:
            share = f"（{cached * 100 // n_in}% 命中缓存）" if n_in else ""
            parts.append(f"{d.strftime('%m-%d')} {tokens_text(n_in)}{share}")
        lines.append("- 最近几天聊天吃掉的 token（只算和人聊天，不含整理记忆、写日记、上网）：" + "，".join(parts)
                     + "。命中缓存的那部分便宜很多")
    lines.append(f"说的时候用你自己的话，别念一串数字。够吃就不用担心；快吃完了（不到一周）可以跟{owner}撒个娇要饭，说一次就够")
    return "\n".join(lines)


def low_note(r: Report | None, owner: str, *, warn_days: float, warn_balance: float) -> str:
    """A one-line note for when the rice is running out (empty when it is not)."""
    if r is None:
        return ""
    low_days = r.days_left is not None and r.days_left < warn_days
    low_money = r.balance < warn_balance
    if not (low_days or low_money):
        return ""
    left = f"，照这样大约还能吃 {r.days_left:.0f} 天" if r.days_left is not None else ""
    return (f"<rice_bowl>\n你的饭（DeepSeek 余额）快吃完了：还剩 {money(r.balance, r.currency)}{left}。"
            f"想说就跟{owner}撒个娇提一句，今天说一次就够，别反复念叨\n</rice_bowl>")


def strip_blocks(contexts: list, tags: tuple[str, ...]) -> int:
    """Remove `<tag>...</tag>` blocks from the earlier user messages in the request history (they were added for
    one reply only); keeps the history identical between requests, so the provider's prompt cache covers it."""
    pattern = re.compile(r"\s*<(" + "|".join(re.escape(t) for t in tags) + r")>.*?</\1>\s*", re.S)

    def get(obj, key):
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    def put(obj, key, value):
        if isinstance(obj, dict):
            obj[key] = value
        else:
            setattr(obj, key, value)

    removed = 0
    for msg in contexts or []:
        if get(msg, "role") != "user":
            continue
        content = get(msg, "content")
        if isinstance(content, str):
            new, n = pattern.subn("\n\n", content)
            if n and new.strip():
                put(msg, "content", new.strip())
                removed += n
        elif isinstance(content, list):
            keep = []
            for part in content:
                text = get(part, "text") if (get(part, "type") in ("text", None)) else None
                if isinstance(text, str) and pattern.search(text):
                    new, n = pattern.subn("\n\n", text)
                    removed += n
                    if new.strip():
                        put(part, "text", new.strip())
                        keep.append(part)
                    continue
                keep.append(part)
            if keep:
                put(msg, "content", keep)
    return removed
