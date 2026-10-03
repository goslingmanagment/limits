#!/usr/bin/env python3
"""Единый экран лимитов: Claude (через claude-swap) + Codex (через CodexBar).

Единый список cswap, CodexBar и профилей Codex из Orca.
Даты подписок: данные входа Codex и отдельно подтверждённые даты Claude.

  python3 watch.py            # живой экран: ↑↓ Enter — переключить Claude, c — стоимость,
                              # r — обновить, q — выход
  python3 watch.py --once     # один кадр и выход (годится для скриптов)
  python3 watch.py --cost     # сколько стоят 1% и 100% окна: цена по API и токены
  python3 watch.py --history  # сколько давали прошлые окна, по датам

Частота опроса: Claude раз в 60 с, Codex раз в 300 с (его чтение дороже).
"""

import argparse
import json
import math
import os
import re
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty
from datetime import datetime, timedelta, timezone
from pathlib import Path

from metadata import (claude_plans, discover_codex, load_subscriptions,
                      parse_date, save_subscription)
from worth import (cache_dir, estimate, load_history, lookup, record_history, summary,
                   window_spend)

CSWAP = os.path.expanduser("~/.local/bin/cswap")
CODEXBAR = "/opt/homebrew/bin/codexbar"
CLAUDE_EVERY = 60
CODEX_EVERY = 300
BAR_WIDTH = 24
# cswap caches usage of inactive accounts for several minutes; only older data is news.
STALE_AFTER = 15 * 60
SOON_DAYS = 5
WEEK = timedelta(days=7)

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
ORANGE = "\033[38;5;214m"
CYAN = "\033[36m"
SELECT = "\033[48;5;237m"
ACTIVE = "  ● активный"
CODEX_DOWN = "CodexBar не отвечает (codexbar usage)"
KEYS = {"\x1b[A": "up", "\x1bOA": "up", "\x1b[B": "down", "\x1bOB": "down",
        "\r": "enter", "\n": "enter", "\x1b": "escape",
        "r": "refresh", "R": "refresh", "к": "refresh", "К": "refresh",
        "c": "cost", "C": "cost", "с": "cost", "С": "cost", "$": "cost",
        "q": "quit", "Q": "quit", "й": "quit", "Й": "quit"}


class FetchError(Exception):
    """A source gave no usable data; the message says why."""


def run(cmd, timeout=180, env=None):
    try:
        # No stdin: a child must never read the keys meant for the live screen.
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env,
                           stdin=subprocess.DEVNULL)
        # CodexBar returns useful per-account JSON even on a partial failure.
        return p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def binary(path, name):
    return path if os.path.exists(path) else (shutil.which(name) or path)


def color_for(pct):
    if pct >= 85:
        return RED
    if pct >= 60:
        return ORANGE
    return GREEN


def percentage(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def span(seconds):
    """'1d 10h' / '3h 33m' / '12m'."""
    d, rem = divmod(max(0, int(seconds)), 86400)
    h, m = rem // 3600, rem % 3600 // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def countdown(resets_at, now=None):
    moment = parse_date(resets_at)
    if not moment:
        return ""
    delta = (moment - (now or datetime.now(timezone.utc))).total_seconds()
    return span(delta) if delta > 0 else "сейчас"


def local_clock(resets_at):
    try:
        s = str(resets_at).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%d.%m %H:%M")
    except Exception:
        return ""


def fetch_claude():
    out = run([binary(CSWAP, "cswap"), "list", "--json"], timeout=120)
    try:
        data = json.loads(out)
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        raise FetchError("claude-swap: " + str(data["error"].get("message") or "ошибка"))
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        raise FetchError("claude-swap не отвечает (cswap list --json)")
    accounts = []
    plans = claude_plans()
    for acc in data.get("accounts", []):
        windows = []
        fresh = acc.get("usage")
        # When a fetch fails cswap still reports the last good usage for display.
        u = fresh if isinstance(fresh, dict) else acc.get("lastGoodUsage")
        u = u if isinstance(u, dict) else {}
        named = [(u.get(key), label, None) for key, label in (("fiveHour", "5h"), ("sevenDay", "7d"))]
        # A model-scoped weekly window counts the usage of that model only.
        named += [(sc, (sc.get("name") or "scoped")[:6], sc.get("name") or "scoped") for sc in u.get("scoped") or []
                  if isinstance(sc, dict)]
        for w, label, scope in named:
            if not isinstance(w, dict) or percentage(w.get("pct")) is None:
                continue
            windows.append(
                {
                    "label": label,
                    "pct": float(w["pct"]),
                    "resets_at": w.get("resetsAt"),
                    "clock": local_clock(w.get("resetsAt")) or w.get("clock", ""),
                    "ahead": bool(w.get("aheadOfPace")),
                    "runs_out_at": (w.get("projectedExhaustionAt")
                                    if w.get("willLastToReset") is False else None),
                    **({"scope": scope} if scope else {}),
                }
            )
        accounts.append(
            {
                "provider": "claude",
                "email": acc.get("email", "?"),
                "note": acc.get("organizationName", ""),
                "org": acc.get("organizationUuid"),
                "active": bool(acc.get("active")),
                "slot": acc.get("number"),
                "plan": plans.get(acc.get("email", "").casefold(), ""),
                "fetched_at": acc.get("usageFetchedAt" if isinstance(fresh, dict) else "lastGoodFetchedAt"),
                "error": "" if isinstance(fresh, dict) and windows else usage_error(acc.get("usageStatus")),
                "windows": windows,
            }
        )
    return accounts


def usage_error(status):
    return {
        "token_expired": "сессия истекла", "relogin_required": "нужен повторный вход",
        "keychain_unavailable": "хранилище входа недоступно",
        "foreign_credential": "данные входа другого аккаунта",
        "no_credentials": "нет данных входа", "api_key": "API-ключ: подписочных лимитов нет",
        "unavailable": "не удалось обновить",
    }.get(status, "лимиты не получены")


def codex_error(error):
    if not error:
        return ""
    message = str(error.get("message", "") if isinstance(error, dict) else error).lower()
    if any(word in message for word in ("login", "auth", "token", "credential", "401")):
        return "нужен повторный вход"
    return "CodexBar: не удалось получить лимиты"


def fetch_codex():
    inventory, homes = discover_codex()
    # A private per-invocation config adds Orca homes without touching CodexBar
    # preferences or changing the account used by other running applications.
    with tempfile.TemporaryDirectory(prefix="limits-") as directory:
        config = Path(directory) / "codexbar.json"
        config.write_text(json.dumps({"version": 1, "providers": [{
            "id": "codex", "enabled": True, "codexProfileHomePaths": homes,
        }]}))
        out = run(
            [binary(CODEXBAR, "codexbar"), "usage", "--provider", "codex",
             "--all-accounts", "--source", "oauth", "--json", "--no-credits"],
            timeout=120, env=dict(os.environ, CODEXBAR_CONFIG=str(config)),
        )
    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        data = []
    accounts = []
    for entry in data if isinstance(data, list) else [data]:
        if not isinstance(entry, dict):
            continue
        u = entry.get("usage") or {}
        pace = entry.get("pace") or {}
        windows = []
        for key, label in (("primary", "5h"), ("secondary", "7d")):
            w = u.get(key)
            if not isinstance(w, dict) or percentage(w.get("usedPercent")) is None:
                continue
            p = pace.get(key) or {}
            eta = percentage(p.get("etaSeconds"))
            windows.append(
                {
                    "label": label,
                    "pct": float(w["usedPercent"]),
                    "resets_at": w.get("resetsAt"),
                    "clock": local_clock(w.get("resetsAt")) or w.get("resetDescription", ""),
                    "ahead": p.get("stage") in ("ahead", "farAhead"),
                    # CodexBar reports the ETA relative to this fetch.
                    "runs_out_at": ((datetime.now(timezone.utc) + timedelta(seconds=eta)).isoformat()
                                    if eta is not None and p.get("willLastToReset") is False else None),
                }
            )
        label = entry.get("account") or ""
        email = u.get("accountEmail") or label.split(" — ")[0] or "codex"
        matches = [a for a in inventory if a["email"].casefold() == email.casefold()]
        # Same email can own several workspaces. Do not attach ambiguous billing
        # dates or combine their usage just because the email matches.
        metadata = matches[0] if len(matches) == 1 else {}
        accounts.append(
            {
                **metadata,
                "provider": "codex",
                "email": email,
                "label": label if len(matches) > 1 else email,
                "note": (u.get("loginMethod") or "").upper(),
                "plan": u.get("loginMethod") or metadata.get("plan", ""),
                "active": False,
                "fetched_at": u.get("updatedAt"),
                "error": codex_error(entry.get("error")) or ("" if windows else "лимиты не получены"),
                "windows": windows,
            }
        )
    seen = {a["email"].casefold() for a in accounts}
    for item in inventory:
        if item["email"].casefold() not in seen:
            accounts.append(dict(item, provider="codex", active=False, windows=[],
                                 fetched_at=None, error="аккаунт не вернулся из CodexBar"))
    return accounts or None


def merge_last_good(old, fresh):
    if fresh is None:
        return old
    def key(acc):
        return (acc["email"], acc.get("account_id") or acc.get("label", ""))
    previous = {key(a): a for a in old or []}
    for acc in fresh:
        before = previous.get(key(acc))
        if acc.get("error") and not acc["windows"] and before:
            acc["windows"] = before["windows"]
            acc["fetched_at"] = before.get("fetched_at")
    return fresh


def cache_path():
    return cache_dir() / "codex.json"


def load_codex_cache():
    """Codex accounts from the previous run, so the screen need not wait for CodexBar."""
    try:
        accounts = json.loads(cache_path().read_text()).get("accounts")
    except (OSError, ValueError, AttributeError):
        return None
    def valid(acc):
        return (isinstance(acc, dict) and acc.get("provider") == "codex" and isinstance(acc.get("email"), str)
                and isinstance(acc.get("windows"), list)
                and all(isinstance(w, dict) and isinstance(w.get("label"), str)
                        and percentage(w.get("pct")) is not None for w in acc["windows"]))
    return accounts if isinstance(accounts, list) and accounts and all(map(valid, accounts)) else None


def save_codex_cache(accounts):
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
            json.dump({"accounts": accounts}, stream, ensure_ascii=False)
        os.replace(stream.name, path)
    except OSError:
        pass


def switch_account(slot):
    out = run([binary(CSWAP, "cswap"), "switch", str(slot), "--json"], timeout=60)
    try:
        data = json.loads(out)
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        raise FetchError("cswap: " + str(data["error"].get("message") or "ошибка"))
    if not isinstance(data, dict) or "switched" not in data:
        raise FetchError("cswap switch не ответил")
    return data


def rolled(window, provider, now):
    """The window as it is now if its reset has already passed.

    Claude weekly limits keep a fixed 7-day cadence (cswap's menu bar rolls them
    the same way). A 5h window, like any Codex window, starts again on first use.
    """
    moment = parse_date(window.get("resets_at"))
    if not moment or moment > now:
        return window
    weekly = provider == "claude" and window["label"] != "5h"
    following = moment + WEEK * ((now - moment) // WEEK + 1) if weekly else None
    return dict(window, pct=0.0, ahead=False, runs_out_at=None,
                resets_at=following and following.isoformat(),
                clock=following and local_clock(following.isoformat()) or "")


def roll_over(accounts, now):
    if accounts is None:
        return None
    return [dict(acc, windows=[rolled(w, acc["provider"], now) for w in acc["windows"]]) for acc in accounts]


def subscription_text(subscription, now=None):
    """Short paid-through date and whether it needs a look; never inferred from limits."""
    until = parse_date((subscription or {}).get("until"))
    if not until:
        return "нет даты подписки", False
    now = now or datetime.now(timezone.utc)
    date_only = len(subscription["until"]) == 10
    day = until.date() if date_only else until.astimezone().date()
    today = now.astimezone().date()
    word = "продление" if subscription.get("kind") == "renews" else "оплачено до"
    text = f"{word} {day.strftime('%d.%m' if day.year == today.year else '%d.%m.%Y')}"
    if (day < today) if date_only else (until <= now):
        return text + " — проверь", True
    days = (day - today).days
    if days > SOON_DAYS:
        return text, False
    return text + (" (сегодня)" if days == 0 else " (завтра)" if days == 1 else f" ({days} дн.)"), True


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def clip_line(line, width):
    """Clip visible cells, preserving whole SGR sequences and a final reset."""
    parts, used = [], 0
    for part in re.split(r"(\x1b\[[0-9;]*m)", line):
        if ANSI.fullmatch(part):
            parts.append(part)
        elif used < width:
            parts.append(part[:width - used])
            used += len(part[:width - used])
    return "".join(parts) + RESET


def visible(text):
    return len(ANSI.sub("", text))


def pad(text, width):
    return text + " " * (width - visible(text))


def shared_domain(accounts):
    """The email domain every account has, so the screen can leave it out."""
    domains = {a["email"].casefold().rpartition("@")[2] if "@" in a["email"] else None for a in accounts}
    return domains.pop() if len(domains) == 1 and None not in domains else ""


def pace_alert(w, now, forecast=True):
    if w["pct"] >= 100:
        return f"{RED}■ исчерпан{RESET}"
    if not forecast:
        return ""
    runs_out = parse_date(w.get("runs_out_at"))
    if runs_out:
        left = (runs_out - now).total_seconds()
        return f"{ORANGE}▲ {'кончится через ~' + span(left) if left > 0 else 'на исходе'}{RESET}"
    if w.get("ahead"):
        return f"{ORANGE}▲ быстрее нормы{RESET}"
    return ""


def notice_text(acc, age, now):
    if not acc["windows"]:
        return acc.get("error") or "лимиты не получены"
    observed = parse_date(acc.get("fetched_at"))
    if observed:
        age = (now - observed).total_seconds()
    if acc.get("error"):
        return acc["error"] + " · последние данные" + (f" {span(age)} назад" if age is not None else "")
    return f"данные {span(age)} назад" if age is not None and age > STALE_AFTER else ""


def identity(acc, domain, subscription, now):
    """Rows of the account column: name, plan, paid-through date."""
    name = acc.get("label") or acc["email"]
    if domain:
        name = re.sub("@" + re.escape(domain), "", name, flags=re.IGNORECASE)
    rows = [f"{BOLD}{name}{RESET}" + (f"{CYAN}{ACTIVE}{RESET}" if acc["active"] else "")]
    plan = acc.get("plan") or ""
    if plan:
        rows.append(f"{DIM}{plan[0].upper() + plan[1:]}{RESET}")
    text, warn = subscription_text(subscription, now)
    return rows + [f"{YELLOW if warn else DIM}{text}{RESET}"]


def bar(pct, width):
    filled = max(0, min(width, round(width * pct / 100)))
    return color_for(pct) + "━" * filled + RESET + DIM + "─" * (width - filled) + RESET


def money(usd):
    if usd < 10:
        return f"${usd:.2f}"
    if usd < 100:
        return f"${usd:.1f}"
    return f"${usd:,.0f}" if usd < 10000 else f"${usd / 1000:.1f}k"


def tokens(n):
    for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= size:
            return f"{n / size:.1f}{unit}" if n < 10 * size else f"{n / size:.0f}{unit}"
    return f"{n:.0f}"


def worth_cells(spent, pct, with_tokens=True):
    """The cost view columns of a window: price and tokens of 1% and of 100% of it."""
    one = estimate(spent, pct)
    if not one:
        return f"{DIM}—{RESET}", f"{DIM}—{RESET}"
    usd, count = one["usd"], one["tokens"]
    def cell(scale, strong):
        amount = money(usd * scale)
        tail = f" · {tokens(count * scale)}" if with_tokens else ""
        if one["rough"]:
            return f"{DIM}~{amount}{tail}{RESET}"
        return f"{BOLD}{amount}{RESET}{tail}" if strong else amount + tail
    return cell(1, False), cell(100, True)


def model_name(model):
    """claude-opus-5-5 -> Opus 5.5, claude-haiku-4-5-20251001 -> Haiku 4.5."""
    parts = model.split("[")[0].casefold().replace("claude-", "", 1).split("-")
    version = ".".join(part for part in parts[1:] if part.isdigit() and len(part) < 4)
    return f"{parts[0].capitalize()} {version}".strip()


def model_shares(models, total):
    """Where a window's spend went: 'Opus 5.5 87% · Fable 5.1 11%'; a single model is just named."""
    ranked = sorted(models.items(), key=lambda item: -item[1]["usd"])
    if total <= 0 or not ranked:
        return ""
    if len(ranked) == 1:
        return model_name(ranked[0][0])
    return " · ".join(f"{model_name(model)} {part['usd'] / total * 100:.0f}%" for model, part in ranked
                      if part["usd"] >= total / 100)


def window_row(w, label_w, middle, reset_w, now):
    pct = w["pct"]
    number = f"{DIM if pct == 0 else BOLD + color_for(pct)}{pct:>3.0f}%{RESET}"
    return (f"{w['label']:<{label_w}}  {number}  {middle}"
            f"   {DIM}{countdown(w.get('resets_at'), now):>{reset_w}}{RESET}")


def render(claude, codex, claude_age, codex_age, errors, subscriptions=None, now=None,
           cursor=None, status="", interactive=False, cost=False, spend=None):
    """The table; with cost, the bars give way to what 1% and 100% of each Claude window are worth."""
    size = shutil.get_terminal_size((120, 40))
    width = max(1, size.columns - 1)
    now = now or datetime.now(timezone.utc)
    subscriptions = subscriptions or {}
    groups = [(title, group, age) for title, group, age in
              (("CLAUDE", claude, claude_age), ("CODEX", codex, codex_age)) if group]
    accounts = [(acc, age) for _, group, age in groups for acc in group]
    domain = shared_domain([acc for acc, _ in accounts])
    idents = [identity(acc, domain, subscriptions.get(acc["provider"] + ":" + acc["email"].casefold(),
                                                     acc.get("subscription")), now)
              for acc, _ in accounts]
    windows = [w for acc, _ in accounts for w in acc["windows"]]
    # Providers project from the average pace since the window opened. On an idle
    # account that never counts down, so only the active Claude account gets it.
    alerts = [[pace_alert(w, now, acc["provider"] == "claude" and acc["active"]) for w in acc["windows"]]
              for acc, _ in accounts]
    if cost:  # the alert column names the models each window's spend went to
        shares = [[model_shares(spent["models"], spent["usd"]) if spent else ""
                   for spent in (acc["provider"] == "claude" and lookup(spend, acc["email"], w)
                                 for w in acc["windows"])] for acc, _ in accounts]
        alerts = [[f"{DIM}{text}{RESET}" if text else "" for text in row] for row in shares]
    def worth(with_tokens):
        cells = [[worth_cells(acc["provider"] == "claude" and lookup(spend, acc["email"], w), w["pct"], with_tokens)
                  for w in acc["windows"]] for acc, _ in accounts]
        return (cells, max([len("за 1%")] + [visible(one) for row in cells for one, _ in row]),
                max([len("за 100%")] + [visible(full) for row in cells for _, full in row]))

    # Columns: slot | account | window label, percent, bar or cost, time to reset | alert.
    # Room for the active badge on every Claude name keeps the table still after a switch.
    name_w = max([visible(row) for rows in idents for row in rows]
                 + [visible(rows[0]) + len(ACTIVE) * (not acc["active"])
                    for (acc, _), rows in zip(accounts, idents) if acc["provider"] == "claude"], default=0)
    label_w = max((len(w["label"]) for w in windows), default=2)
    reset_w = max((len(countdown(w.get("resets_at"), now)) for w in windows), default=0)
    left_w = 5 + name_w + 3
    def fixed_width():
        alert_w = max((visible(alert) for row in alerts for alert in row), default=0)
        return alert_w, left_w + label_w + 2 + 4 + 2 + 3 + reset_w + (3 + alert_w if alert_w else 0)
    cells, one_w, full_w = worth(True) if cost else ([], 0, 0)
    if cost and fixed_width()[1] + one_w + 3 + full_w > width:
        # A narrow terminal gives up the models first, then the token counts; prices stay.
        alerts = [[""] * len(row) for row in alerts]
        if fixed_width()[1] + one_w + 3 + full_w > width:
            cells, one_w, full_w = worth(False)
    alert_w, fixed = fixed_width()
    middle_w = one_w + 3 + full_w if cost else max(10, min(BAR_WIDTH, width - fixed))
    alerts_inline = fixed + middle_w <= width

    blocks, chosen = [], None
    for n, ((acc, age), rows, account_alerts) in enumerate(zip(accounts, idents, alerts)):
        right = []
        for k, (w, alert) in enumerate(zip(acc["windows"], account_alerts)):
            middle = (pad(cells[n][k][0], one_w) + "   " + pad(cells[n][k][1], full_w) if cost
                      else bar(w["pct"], middle_w))
            right.append(window_row(w, label_w, middle, reset_w, now))
            if alert and alerts_inline:
                right[-1] += "   " + alert
            elif alert:
                right.append(" " * (label_w + 8) + alert)
        notice = notice_text(acc, age, now)
        if notice:
            right.append(f"{YELLOW}{notice}{RESET}")
        slot = str(acc.get("slot") or "") if acc["provider"] == "claude" else ""
        if slot and acc.get("slot") == cursor:
            chosen = len(blocks)
        mark = "▶ " if chosen == len(blocks) else "  "
        blocks.append([
            (f"{BOLD}{mark}{slot:<2}{RESET} " if i == 0 else " " * 5)
            + pad(rows[i] if i < len(rows) else "", name_w) + "   " + (right[i] if i < len(right) else "")
            for i in range(max(len(rows), len(right)))
        ])

    if chosen is not None:
        # The selected account sits on a band as wide as the table.
        table_w = max(visible(line) for block in blocks for line in block)
        blocks[chosen] = [SELECT + line.replace(RESET, RESET + SELECT) + " " * (table_w - visible(line)) + RESET
                          for line in blocks[chosen]]

    middle_at = left_w + label_w + 2 + 4 + 2
    reset_end = middle_at + middle_w + 3 + reset_w
    def title_line(title):
        line = f"  {BOLD}{title}{RESET}"
        if cost and title == "CLAUDE":
            for header, at in (("за 1%", middle_at), ("за 100%", middle_at + one_w + 3)):
                line += " " * max(2, at - visible(line)) + f"{DIM}{header}{RESET}"
        if not reset_w:
            return line
        line += " " * max(2, reset_end - 11 - visible(line)) + f"{DIM}сброс через{RESET}"
        return line + f"   {DIM}модели{RESET}" if cost and title == "CLAUDE" and alerts_inline and alert_w else line

    hint = (f"↑↓ выбрать · Enter переключить · c {'лимиты' if cost else 'стоимость'} · r обновить · q выход"
            if interactive else "переключить Claude: cswap switch <номер>")
    legend = [f"  {DIM}цена по API и токены вместе с кэшем; считается расход на этом Mac — "
              f"claude.ai и телефон не видны · ~ окно < 10%{RESET}"] if cost else []
    footer = [""] + [f"  {RED}{e}{RESET}" for e in errors] + ([f"  {status}"] if status else []) + legend + [
        f"  {DIM}{hint}   ·   обновлено {now.astimezone():%H:%M}{RESET}"]
    # A blank line before every account only when everything still fits on the screen.
    tight = 2 * len(groups) - 1 + sum(map(len, blocks)) + len(footer)
    spacious = tight + len(blocks) < size.lines
    lines, i = [], 0
    for title, group, _ in groups:
        if lines:
            lines.append("")
        lines.append(title_line(title))
        for block in blocks[i:i + len(group)]:
            if spacious:
                lines.append("")
            lines += block
        i += len(group)
    if not accounts:
        lines.append(f"  {RED}нет данных{RESET}")
    return "\n".join(clip_line(line, width) for line in lines + footer)


def short_name(acc):
    return acc["email"].split("@")[0]


def tab_title(claude, now=None):
    active = next((a for a in claude or [] if a["active"] and a["windows"]), None)
    if not active:
        return "limits"
    now = now or datetime.now(timezone.utc)
    top = max(w["pct"] for w in active["windows"])
    alarm = any(pace_alert(w, now) for w in active["windows"])
    return f"limits · {short_name(active)} {top:.0f}%" + (" ▲" if alarm else "")


def keys(data):
    """Key names in one read from the terminal; digits stay digits, the rest is dropped."""
    text, found = data.decode("utf-8", "ignore"), []
    while text:
        chunk = next((text[:size] for size in (3, 1) if text[:size] in KEYS), None)
        if chunk:
            found.append(KEYS[chunk])
        elif text[0].isdigit():
            found.append(text[0])
        text = text[len(chunk or text[0]):]
    return found


def move(cursor, key, claude):
    """The slot selected after an arrow, digit or Esc; None means nothing is selected."""
    slots = [acc["slot"] for acc in claude or [] if acc.get("slot")]
    if key == "escape" or not slots:
        return None
    if key.isdigit():
        return int(key) if int(key) in slots else cursor
    # The first arrow press steps away from the active account.
    start = cursor if cursor in slots else next((a["slot"] for a in claude if a["active"] and a.get("slot")), None)
    if start is None:
        return slots[0]
    step = slots.index(start) + (1 if key == "down" else -1)
    return slots[max(0, min(len(slots) - 1, step))]


def background(job):
    """Run a job in a daemon thread: quitting never waits for a slow CLI."""
    box = {}
    def work():
        try:
            box["result"] = job()
        except Exception as error:
            box["error"] = error
    box["thread"] = threading.Thread(target=work, daemon=True)
    box["thread"].start()
    return box


def switch_note(result, error):
    if error is not None:
        return f"{RED}✗ {error}{RESET}"
    to = result.get("to") or {}
    if result.get("switched"):
        return f"{GREEN}✓ Переключено на {to.get('number', '')} {str(to.get('email', '')).split('@')[0]}{RESET}"
    return f"{DIM}{result.get('message') or 'без изменений'}{RESET}"


def measure(accounts):
    """Spend in each Claude window; every estimate that has moved on also goes to the history."""
    spend = window_spend(accounts)
    record_history(accounts, spend)
    return spend


def history_report(entries, now=None):
    """The final estimate of every window in the history, by window and plan, oldest first."""
    now = now or datetime.now(timezone.utc)
    groups = {}
    for entry in entries:
        if entry.get("per_percent"):
            groups.setdefault((entry["window"] != "7d", entry["window"] == "5h", entry["window"],
                               entry.get("plan") or ""), []).append(entry)
    if not groups:
        return "  история пуста: она пишется, пока открыт limits или вызывается limits --cost"
    lines = []
    for (_, five, label, plan), group in sorted(groups.items()):
        # Short windows pile up; the latest dozen measured past rounding say enough.
        group = [e for e in group if not e.get("rough")][-12:] if five else group
        rows = []
        for e in group:
            resets = parse_date(e["resets_at"])
            one, full = e["per_percent"], e["full"]
            mark = "~" if e.get("rough") else ""
            rows.append((f"{resets.astimezone():%d.%m %H:%M}" + (" идёт" if resets > now else ""),
                         e["email"].split("@")[0], f"{e['pct']:.0f}%",
                         f"{mark}{money(one['usd'])} · {tokens(one['tokens'])}",
                         f"{mark}{money(full['usd'])} · {tokens(full['tokens'])}",
                         model_shares(e.get("models") or {}, e.get("spent_usd") or 0)))
        head = ("окно до", "аккаунт", "%", "за 1%", "за 100%", "модели")
        widths = [max(len(row[i]) for row in rows + [head]) for i in range(5)]
        lines += ["", f"  {BOLD}{label} · {plan or 'тариф неизвестен'}{RESET}",
                  f"  {DIM}" + "   ".join(h.ljust(w) if i != 2 else h.rjust(w) for i, (h, w) in
                                           enumerate(zip(head, widths + [0]))).rstrip() + RESET]
        for row in rows:
            lines.append("  " + "   ".join(c.ljust(w) if i != 2 else c.rjust(w)
                                           for i, (c, w) in enumerate(zip(row, widths + [0]))).rstrip())
    return "\n".join(lines[1:])


def snapshot(args, subscriptions, color):
    try:
        claude, claude_error = fetch_claude(), ""
    except FetchError as error:
        claude, claude_error = None, str(error)
    codex = fetch_codex()
    if codex is not None:
        save_codex_cache(codex)
    errors = [message for message in (claude_error, "" if codex is not None else CODEX_DOWN) if message]
    moment = datetime.now(timezone.utc)
    claude, codex = roll_over(claude, moment), roll_over(codex, moment)
    accounts = (claude or []) + (codex or [])
    spend = None
    if args.cost and claude:
        try:
            spend = measure(claude)
        except (OSError, sqlite3.Error) as error:
            errors.append(f"расход не посчитан: {error}")
    if args.json:
        for acc in accounts:
            acc["subscription"] = subscriptions.get(
                acc["provider"] + ":" + acc["email"].casefold(), acc.get("subscription"))
            for w in acc["windows"] if spend is not None and acc["provider"] == "claude" else []:
                w["cost"] = summary(lookup(spend, acc["email"], w), w["pct"])
        print(json.dumps({"accounts": accounts, "errors": errors}, ensure_ascii=False, indent=2))
    else:
        frame = render(claude, codex, 0.0, 0.0, errors, subscriptions, cost=args.cost, spend=spend)
        print(frame if color else ANSI.sub("", frame))
    return 0 if any(a["windows"] for a in accounts) else 1


def live(subscriptions, color, cost=False):
    """The live screen: sources refresh in the background, keys switch the Claude account and the view."""
    fd = sys.stdin.fileno() if sys.stdin.isatty() else None
    saved = termios.tcgetattr(fd) if fd is not None else None
    if fd is not None:
        tty.setcbreak(fd)
    # Like any full-screen tool: own screen, no cursor; the shell's screen returns on exit.
    sys.stdout.write("\033[?1049h\033[?25l")
    try:
        sources = {"claude": (fetch_claude, CLAUDE_EVERY), "codex": (fetch_codex, CODEX_EVERY)}
        data = {"claude": None, "codex": load_codex_cache()}
        fetched = {"claude": 0.0, "codex": 0.0}
        failed = {"claude": "", "codex": "", "spend": ""}
        # Usage is measured in the background with every view, so the history keeps growing;
        # the very first pass indexes a week of transcripts.
        spend, spent_at = None, 0.0
        jobs, cursor, note, note_until, refreshing = {}, None, "", 0.0, False
        began, drawn_at, drawn_size = time.time(), 0.0, None
        while True:
            changed = False
            for name, (fetch, every) in sources.items():
                if name not in jobs and time.time() - fetched[name] >= every:
                    jobs[name] = background(fetch)
            if data["claude"] and "spend" not in jobs and time.time() - spent_at >= CLAUDE_EVERY:
                jobs["spend"] = background(lambda accounts=data["claude"]: measure(accounts))
            for name in [name for name, job in jobs.items() if not job["thread"].is_alive()]:
                job, changed = jobs.pop(name), True
                result, error = job.get("result"), job.get("error")
                if name == "switch":
                    # Move the active marker now; asking cswap again at once confirms it.
                    if result and result.get("switched"):
                        slot = (result.get("to") or {}).get("number")
                        data["claude"] = [dict(a, active=a.get("slot") == slot) for a in data["claude"] or []]
                    cursor, fetched["claude"] = None, 0.0
                    note, note_until = switch_note(result, error), time.time() + 8
                    continue
                if name == "spend":
                    spent_at, spend = time.time(), spend if error else result
                    failed[name] = f"расход не посчитан: {error}" if error else ""
                    continue
                fetched[name] = time.time()
                if name == "claude":
                    failed[name] = "" if error is None else str(error) if isinstance(error, FetchError) \
                        else f"claude-swap: {error}"
                else:
                    failed[name] = "" if result is not None else f"CodexBar: {error}" if error else CODEX_DOWN
                data[name] = merge_last_good(data[name], result)
                if name == "codex" and result is not None:
                    save_codex_cache(data["codex"])
            refreshing = refreshing and ("claude" in jobs or "codex" in jobs)

            ready = select.select([fd], [], [], 0.2)[0] if fd is not None else time.sleep(0.2)
            pressed = os.read(fd, 64) if ready else None
            if pressed == b"":
                return 0  # the terminal is gone
            for key in keys(pressed) if pressed else []:
                changed = True
                target = next((a for a in data["claude"] or [] if a.get("slot") == cursor), None)
                if key == "quit":
                    return 0
                if key == "refresh":
                    fetched, spent_at, refreshing = dict.fromkeys(fetched, 0.0), 0.0, True
                elif key == "cost":
                    cost = not cost
                elif key == "enter" and target and "switch" not in jobs:
                    if target["active"]:
                        cursor, note, note_until = None, f"{short_name(target)} уже активный", time.time() + 5
                    else:
                        jobs["switch"] = background(lambda slot=cursor: switch_account(slot))
                        note, note_until = f"переключаю на {cursor} {short_name(target)}…", float("inf")
                elif key != "enter":
                    cursor = move(cursor, key, data["claude"])

            size = shutil.get_terminal_size()
            if data["claude"] is None and "claude" in jobs and time.time() - began < 1:
                continue  # cswap answers in a moment; do not flash an empty screen
            if not (changed or size != drawn_size or time.time() - drawn_at >= 10):
                continue
            target = next((a for a in data["claude"] or [] if a.get("slot") == cursor), None)
            if target and "switch" not in jobs:
                status = (f"{short_name(target)} уже активный · Esc — отмена" if target["active"] else
                          f"Enter — переключиться на {cursor} {short_name(target)} · Esc — отмена")
            elif note and time.time() < note_until:
                status = note
            elif refreshing:
                status = f"{DIM}обновляю…{RESET}"
            elif cost and spend is None and "spend" in jobs:
                status = f"{DIM}считаю расход по транскриптам…{RESET}"
            elif data["codex"] is None and "codex" in jobs:
                status = f"{DIM}Codex: загрузка…{RESET}"
            else:
                status = ""
            moment = datetime.now(timezone.utc)
            claude, codex = roll_over(data["claude"], moment), roll_over(data["codex"], moment)
            ages = {name: time.time() - fetched[name] if fetched[name] else None for name in fetched}
            errors = [m for name, m in failed.items() if m and (cost or name != "spend")]
            frame = render(claude, codex, ages["claude"], ages["codex"], errors,
                           subscriptions, cursor=cursor, status=status, interactive=fd is not None,
                           cost=cost, spend=spend)
            if not color:
                frame = ANSI.sub("", frame)
            # Overwrite in place inside a synchronized update instead of clearing: no flicker.
            body = "\n".join(line + "\033[K" for line in frame.split("\n"))
            sys.stdout.write(f"\033]2;{tab_title(claude)}\007\033[?2026h\033[H{body}\033[J\033[?2026l")
            sys.stdout.flush()
            drawn_at, drawn_size = time.time(), size
    finally:
        if fd is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        # An empty title hands the tab back to the shell's own title.
        sys.stdout.write("\033[?25h\033[?1049l\033]2;\007")
        sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description="Лимиты и подписки Claude + Codex (включая профили Orca)")
    parser.add_argument("--once", action="store_true", help="один кадр и выход")
    parser.add_argument("--json", action="store_true", help="один снимок JSON")
    parser.add_argument("--no-color", action="store_true", help="без ANSI-цветов")
    parser.add_argument("--cost", action="store_true",
                        help="сколько стоят 1%% и 100%% окна Claude: цена по API и токены локального расхода")
    parser.add_argument("--history", action="store_true", help="итог каждого прошлого окна Claude по датам")
    parser.add_argument("--set-subscription", nargs=3, metavar=("PROVIDER", "EMAIL", "YYYY-MM-DD"),
                        help="запомнить подтверждённую дату; не продлевает подписку")
    parser.add_argument("--renews", action="store_true", help="дата означает автопродление")
    parser.add_argument("--subscription-source", default="вручную", help="источник подтверждённой даты")
    args = parser.parse_args()
    try:
        if args.set_subscription:
            save_subscription(*args.set_subscription, renews=args.renews, source=args.subscription_source)
            print("Дата подписки сохранена.")
            return 0
        subscriptions = load_subscriptions()
    except (OSError, ValueError) as error:
        print(f"Не удалось прочитать/сохранить даты подписок: {error}", file=sys.stderr)
        return 2
    once = args.once or args.json or not sys.stdout.isatty()
    color = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
    if args.history:
        report = history_report(load_history())
        print(report if color else ANSI.sub("", report))
        return 0
    try:
        return snapshot(args, subscriptions, color) if once else live(subscriptions, color, args.cost)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
