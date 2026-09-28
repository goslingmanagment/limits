#!/usr/bin/env python3
"""Единый экран лимитов: Claude (через claude-swap) + Codex (через CodexBar).

Единый список cswap, CodexBar и профилей Codex из Orca.
Даты подписок: данные входа Codex и отдельно подтверждённые даты Claude.

  python3 watch.py            # живой экран, Ctrl-C выход
  python3 watch.py --once     # один кадр и выход (годится для скриптов)

Частота опроса: Claude раз в 60 с, Codex раз в 300 с (его чтение дороже).
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from metadata import (claude_plans, discover_codex, load_subscriptions,
                      parse_date, save_subscription)

CSWAP = os.path.expanduser("~/.local/bin/cswap")
CODEXBAR = "/opt/homebrew/bin/codexbar"
CLAUDE_EVERY = 60
CODEX_EVERY = 300
BAR_WIDTH = 30

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
ORANGE = "\033[38;5;214m"
CYAN = "\033[36m"


def run(cmd, timeout=180, env=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
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


def countdown(resets_at):
    """'3d 5h' / '1h 14m' из ISO-времени."""
    if not resets_at:
        return ""
    try:
        s = str(resets_at).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return ""
    delta = int((dt - datetime.now(timezone.utc)).total_seconds())
    if delta <= 0:
        return "сброс"
    d, rem = divmod(delta, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


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
    if not out:
        return None
    try:
        data = json.loads(out)
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        return None
    accounts = []
    plans = claude_plans()
    for acc in data.get("accounts", []):
        windows = []
        u = acc.get("usage") or {}
        for key, label in (("fiveHour", "5h"), ("sevenDay", "7d")):
            w = u.get(key)
            if not isinstance(w, dict) or percentage(w.get("pct")) is None:
                continue
            windows.append(
                {
                    "label": label,
                    "pct": float(w["pct"]),
                    "resets_at": w.get("resetsAt"),
                    "clock": local_clock(w.get("resetsAt")) or w.get("clock", ""),
                    "ahead": bool(w.get("aheadOfPace")),
                }
            )
        for sc in u.get("scoped") or []:
            if not isinstance(sc, dict) or percentage(sc.get("pct")) is None:
                continue
            windows.append(
                {
                    "label": (sc.get("name") or "scoped")[:6],
                    "pct": float(sc["pct"]),
                    "resets_at": sc.get("resetsAt"),
                    "clock": local_clock(sc.get("resetsAt")) or sc.get("clock", ""),
                    "ahead": bool(sc.get("aheadOfPace")),
                }
            )
        accounts.append(
            {
                "provider": "claude",
                "email": acc.get("email", "?"),
                "note": acc.get("organizationName", ""),
                "active": bool(acc.get("active")),
                "slot": acc.get("number"),
                "plan": plans.get(acc.get("email", "").casefold(), ""),
                "fetched_at": acc.get("usageFetchedAt"),
                "error": usage_error(acc.get("usageStatus")) if not windows else "",
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
            windows.append(
                {
                    "label": label,
                    "pct": float(w["usedPercent"]),
                    "resets_at": w.get("resetsAt"),
                    "clock": local_clock(w.get("resetsAt")) or w.get("resetDescription", ""),
                    "ahead": (pace.get(key) or {}).get("stage") in ("ahead", "farAhead"),
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


def subscription_text(subscription, now=None):
    if not subscription or not parse_date(subscription.get("until")):
        return "подписка: дата неизвестна"
    now = now or datetime.now(timezone.utc)
    value = subscription["until"]
    until = parse_date(value)
    date_only = len(value) == 10
    days = ((until.date() if date_only else until.astimezone().date()) - now.astimezone().date()).days
    date_text = until.strftime("%d.%m.%Y") if date_only else until.astimezone().strftime("%d.%m.%Y %H:%M")
    word = "продление" if subscription.get("kind") == "renews" else "период до"
    passed = days < 0 if date_only else until <= now
    remaining = "дата прошла — проверь" if passed else f"через {days} дн." if days > 0 else "сегодня"
    checked = parse_date(subscription.get("checked_at"))
    evidence = ""
    if checked:
        prefix = "вход" if subscription.get("source") == "вход Codex" else "проверено"
        evidence = f" · {prefix} {checked.astimezone():%d.%m}"
    return f"{word} {date_text} · {remaining}{evidence}"


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


def render(claude, codex, claude_age, codex_age, errors, subscriptions=None):
    size = shutil.get_terminal_size((120, 40))
    width = size.columns
    subscriptions = subscriptions or {}
    all_accounts = (claude or []) + (codex or [])
    # Compact mode keeps all five accounts visible on a 24-line terminal.
    needed = 3 + sum(2 + max(1, len(a["windows"])) + bool(a.get("error")) for a in all_accounts)
    spacious = needed + len(all_accounts) < size.lines
    lines = []
    lines.append(f"{DIM}все аккаунты · claude + codex{RESET}")
    if spacious:
        lines.append("")

    n = 0
    for acc in all_accounts:
        n += 1
        tag = f"{CYAN}claude{RESET}" if acc["provider"] == "claude" else f"{YELLOW}codex{RESET}"
        age = claude_age if acc["provider"] == "claude" else codex_age
        slot = acc.get("slot") or n
        plan = acc.get("plan", "")
        head = f" {BOLD}{slot}{RESET}  {BOLD}{acc.get('label') or acc['email']}{RESET}  [{tag}]"
        if plan:
            head += f" {DIM}{plan}{RESET}"
        if acc["active"]:
            head += f"  {ORANGE}● active{RESET}"
        observed = parse_date(acc.get("fetched_at"))
        if observed:
            age = (datetime.now(timezone.utc) - observed).total_seconds()
        stale_after = 120 if acc["provider"] == "claude" else 600
        if age is not None and age > stale_after:
            head += f"  {DIM}· {int(age // 60)}m назад{RESET}"
        lines.append(head)
        sub = subscriptions.get(acc["provider"] + ":" + acc["email"].casefold(), acc.get("subscription"))
        lines.append(f"    {DIM}{subscription_text(sub)}{RESET}")
        if acc.get("error"):
            lines.append(f"    {YELLOW}{acc['error']}{' · последние данные' if acc['windows'] else ''}{RESET}")
        for w in acc["windows"]:
            pct = w["pct"]
            col = color_for(pct)
            bar_width = max(8, min(BAR_WIDTH, width - 64))
            filled = max(0, min(bar_width, round(bar_width * pct / 100)))
            bar = col + "━" * filled + RESET + DIM + "─" * (bar_width - filled) + RESET
            cd = countdown(w["resets_at"])
            tail = f"{DIM}сброс {cd} · {w['clock']}{RESET}" if cd else f"{DIM}{w['clock']}{RESET}"
            ahead = f"  {ORANGE}↑ темп{RESET}" if w["ahead"] else ""
            lines.append(f"    {w['label']:<6}{bar} {col}{pct:>4.0f}%{RESET}  {tail}{ahead}")
        if not acc["windows"] and not acc.get("error"):
            lines.append(f"    {DIM}лимиты не получены{RESET}")
        if spacious:
            lines.append("")

    if not n:
        lines.append(f"  {RED}нет данных{RESET}")
        lines.append("")
    for e in errors:
        lines.append(f"  {RED}{e}{RESET}")
    if errors:
        lines.append("")
    lines.append(f"{DIM}Claude: cswap switch <слот> · даты: limits --help · выход Ctrl-C{RESET}")
    return "\n".join(clip_line(line, max(1, width - 1)) for line in lines)


def main():
    parser = argparse.ArgumentParser(description="Лимиты и подписки Claude + Codex (включая профили Orca)")
    parser.add_argument("--once", action="store_true", help="один кадр и выход")
    parser.add_argument("--json", action="store_true", help="один снимок JSON")
    parser.add_argument("--no-color", action="store_true", help="без ANSI-цветов")
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
    claude = codex = None
    claude_at = codex_at = 0.0
    failed = {"claude": False, "codex": False}

    try:
        while True:
            now = time.time()
            if not claude_at or now - claude_at >= CLAUDE_EVERY:
                fresh = fetch_claude()
                failed["claude"] = fresh is None
                claude, claude_at = merge_last_good(claude, fresh), time.time()
            if not codex_at or now - codex_at >= CODEX_EVERY:
                fresh = fetch_codex()
                failed["codex"] = fresh is None
                codex, codex_at = merge_last_good(codex, fresh), time.time()
            errors = []
            if failed["claude"]:
                errors.append("claude-swap не отвечает (cswap list --json)")
            if failed["codex"]:
                errors.append("CodexBar не отвечает (codexbar usage)")
            if args.json:
                accounts = (claude or []) + (codex or [])
                for acc in accounts:
                    acc["subscription"] = subscriptions.get(
                        acc["provider"] + ":" + acc["email"].casefold(), acc.get("subscription"))
                print(json.dumps({"accounts": accounts, "errors": errors}, ensure_ascii=False, indent=2))
                return 0 if any(a["windows"] for a in accounts) else 1
            frame = render(
                claude,
                codex,
                time.time() - claude_at if claude else None,
                time.time() - codex_at if codex else None,
                errors,
                subscriptions,
            )
            if not color:
                frame = ANSI.sub("", frame)
            if once:
                print(frame)
                return 0 if any(a["windows"] for a in (claude or []) + (codex or [])) else 1
            sys.stdout.write("\033[H\033[2J" + frame + "\n")
            sys.stdout.flush()
            time.sleep(10)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
