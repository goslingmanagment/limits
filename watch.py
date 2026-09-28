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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from metadata import (claude_plans, discover_codex, load_subscriptions,
                      parse_date, save_subscription)

CSWAP = os.path.expanduser("~/.local/bin/cswap")
CODEXBAR = "/opt/homebrew/bin/codexbar"
CLAUDE_EVERY = 60
CODEX_EVERY = 300
BAR_WIDTH = 24
# cswap caches usage of inactive accounts for several minutes; only older data is news.
STALE_AFTER = 15 * 60
SOON_DAYS = 5

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
        named = [(u.get(key), label) for key, label in (("fiveHour", "5h"), ("sevenDay", "7d"))]
        named += [(sc, (sc.get("name") or "scoped")[:6]) for sc in u.get("scoped") or []
                  if isinstance(sc, dict)]
        for w, label in named:
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
    rows = [f"{BOLD}{name}{RESET}" + (f"  {CYAN}● активный{RESET}" if acc["active"] else "")]
    plan = acc.get("plan") or ""
    if plan:
        rows.append(f"{DIM}{plan[0].upper() + plan[1:]}{RESET}")
    text, warn = subscription_text(subscription, now)
    return rows + [f"{YELLOW if warn else DIM}{text}{RESET}"]


def window_row(w, label_w, bar_w, reset_w, now):
    pct = w["pct"]
    col = color_for(pct)
    filled = max(0, min(bar_w, round(bar_w * pct / 100)))
    bar = col + "━" * filled + RESET + DIM + "─" * (bar_w - filled) + RESET
    number = f"{DIM if pct == 0 else BOLD + col}{pct:>3.0f}%{RESET}"
    return (f"{w['label']:<{label_w}}  {number}  {bar}"
            f"   {DIM}{countdown(w.get('resets_at'), now):>{reset_w}}{RESET}")


def render(claude, codex, claude_age, codex_age, errors, subscriptions=None, now=None):
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

    # Columns: slot | account | window label, percent, bar, time to reset | alert.
    name_w = max((visible(row) for rows in idents for row in rows), default=0)
    label_w = max((len(w["label"]) for w in windows), default=2)
    reset_w = max((len(countdown(w.get("resets_at"), now)) for w in windows), default=0)
    alert_w = max((visible(alert) for row in alerts for alert in row), default=0)
    left_w = 5 + name_w + 3
    fixed = left_w + label_w + 2 + 4 + 2 + 3 + reset_w + (3 + alert_w if alert_w else 0)
    bar_w = max(10, min(BAR_WIDTH, width - fixed))
    alerts_inline = fixed + bar_w <= width

    blocks = []
    for (acc, age), rows, account_alerts in zip(accounts, idents, alerts):
        right = []
        for w, alert in zip(acc["windows"], account_alerts):
            right.append(window_row(w, label_w, bar_w, reset_w, now))
            if alert and alerts_inline:
                right[-1] += "   " + alert
            elif alert:
                right.append(" " * (label_w + 8) + alert)
        notice = notice_text(acc, age, now)
        if notice:
            right.append(f"{YELLOW}{notice}{RESET}")
        slot = str(acc.get("slot") or "") if acc["provider"] == "claude" else ""
        blocks.append([
            (f"  {BOLD}{slot:<2}{RESET} " if i == 0 else " " * 5)
            + pad(rows[i] if i < len(rows) else "", name_w) + "   " + (right[i] if i < len(right) else "")
            for i in range(max(len(rows), len(right)))
        ])

    reset_end = left_w + label_w + 2 + 4 + 2 + bar_w + 3 + reset_w
    def title_line(title):
        line = f"  {BOLD}{title}{RESET}"
        if not reset_w:
            return line
        return line + " " * max(2, reset_end - 11 - visible(line)) + f"{DIM}сброс через{RESET}"

    footer = [""] + [f"  {RED}{e}{RESET}" for e in errors] + [
        f"  {DIM}переключить Claude: cswap switch <номер>   ·   "
        f"обновлено {now.astimezone():%H:%M}   ·   выход Ctrl-C{RESET}"]
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


def tab_title(claude, now=None):
    active = next((a for a in claude or [] if a["active"] and a["windows"]), None)
    if not active:
        return "limits"
    now = now or datetime.now(timezone.utc)
    top = max(w["pct"] for w in active["windows"])
    alarm = any(pace_alert(w, now) for w in active["windows"])
    return f"limits · {active['email'].split('@')[0]} {top:.0f}%" + (" ▲" if alarm else "")


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
            sys.stdout.write(f"\033]2;{tab_title(claude)}\007\033[H\033[2J" + frame + "\n")
            sys.stdout.flush()
            time.sleep(10)
    except KeyboardInterrupt:
        # An empty title hands the tab back to the shell's own title.
        sys.stdout.write("\n" if once else "\033]2;\007\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
