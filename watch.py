#!/usr/bin/env python3
"""Единый экран лимитов: Claude (через claude-swap) + Codex (через CodexBar).

Вид повторяет `cswap watch`, но в одном списке и с Codex.

  python3 watch.py            # живой экран, Ctrl-C выход
  python3 watch.py --once     # один кадр и выход (годится для скриптов)

Частота опроса: Claude раз в 60 с, Codex раз в 300 с (его чтение дороже).
"""

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

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


def run(cmd, timeout=180):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else ""
    except Exception:
        return ""


def binary(path, name):
    return path if os.path.exists(path) else (shutil.which(name) or path)


def color_for(pct):
    if pct >= 85:
        return RED
    if pct >= 60:
        return ORANGE
    return GREEN


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
        return dt.astimezone().strftime("%b %d %H:%M")
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
    accounts = []
    for acc in data.get("accounts", []):
        windows = []
        u = acc.get("usage") or {}
        for key, label in (("fiveHour", "5h"), ("sevenDay", "7d")):
            w = u.get(key)
            if not w or w.get("pct") is None:
                continue
            windows.append(
                {
                    "label": label,
                    "pct": float(w["pct"]),
                    "resets_at": w.get("resetsAt"),
                    "clock": w.get("clock") or local_clock(w.get("resetsAt")),
                    "ahead": bool(w.get("aheadOfPace")),
                }
            )
        for sc in u.get("scoped") or []:
            if sc.get("pct") is None:
                continue
            windows.append(
                {
                    "label": (sc.get("name") or "scoped")[:6],
                    "pct": float(sc["pct"]),
                    "resets_at": sc.get("resetsAt"),
                    "clock": sc.get("clock") or local_clock(sc.get("resetsAt")),
                    "ahead": bool(sc.get("aheadOfPace")),
                }
            )
        accounts.append(
            {
                "provider": "claude",
                "email": acc.get("email", "?"),
                "note": acc.get("organizationName", ""),
                "active": bool(acc.get("active")),
                "windows": windows,
            }
        )
    return accounts


def fetch_codex():
    out = run(
        [binary(CODEXBAR, "codexbar"), "usage", "--provider", "codex", "--all-accounts", "--json"],
        timeout=240,
    )
    if not out:
        return None
    try:
        data = json.loads(out)
    except Exception:
        return None
    accounts = []
    for entry in data if isinstance(data, list) else [data]:
        u = entry.get("usage") or {}
        pace = entry.get("pace") or {}
        windows = []
        for key, label in (("primary", "5h"), ("secondary", "7d")):
            w = u.get(key)
            if not w or w.get("usedPercent") is None:
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
        if not windows:
            continue
        accounts.append(
            {
                "provider": "codex",
                "email": u.get("accountEmail") or "codex",
                "note": (u.get("loginMethod") or "").upper(),
                "active": False,
                "windows": windows,
            }
        )
    return accounts


def render(claude, codex, claude_age, codex_age, errors):
    width = shutil.get_terminal_size((100, 30)).columns
    lines = []
    lines.append(f"{DIM}все аккаунты · claude + codex{RESET}")
    lines.append("")

    n = 0
    for acc in (claude or []) + (codex or []):
        n += 1
        tag = f"{CYAN}claude{RESET}" if acc["provider"] == "claude" else f"{YELLOW}codex{RESET}"
        age = claude_age if acc["provider"] == "claude" else codex_age
        head = f" {BOLD}{n}{RESET}  {BOLD}{acc['email']}{RESET}  [{tag}]"
        if acc["active"]:
            head += f"  {ORANGE}● active{RESET}"
        if age is not None and age > 90:
            head += f"  {DIM}· {int(age // 60)}m назад{RESET}"
        lines.append(head)
        for w in acc["windows"]:
            pct = w["pct"]
            col = color_for(pct)
            filled = max(0, min(BAR_WIDTH, round(BAR_WIDTH * pct / 100)))
            bar = col + "━" * filled + RESET + DIM + "─" * (BAR_WIDTH - filled) + RESET
            cd = countdown(w["resets_at"])
            tail = f"{DIM}сброс {cd} · {w['clock']}{RESET}" if cd else f"{DIM}{w['clock']}{RESET}"
            ahead = f"  {ORANGE}(с опережением){RESET}" if w["ahead"] else ""
            lines.append(f"    {w['label']:<6}{bar} {col}{pct:>4.0f}%{RESET}  {tail}{ahead}")
        lines.append("")

    if not n:
        lines.append(f"  {RED}нет данных{RESET}")
        lines.append("")
    for e in errors:
        lines.append(f"  {RED}{e}{RESET}")
    if errors:
        lines.append("")
    lines.append(f"{DIM}переключить Claude: cswap switch <номер слота> · Codex: CodexBar → Settings → Codex → System · выход Ctrl-C{RESET}")
    return "\n".join(line[: width + 200] for line in lines)


def main():
    once = "--once" in sys.argv
    claude = codex = None
    claude_at = codex_at = 0.0
    errors = []

    try:
        while True:
            now = time.time()
            errors = []
            if claude is None or now - claude_at >= CLAUDE_EVERY:
                fresh = fetch_claude()
                if fresh is not None:
                    claude, claude_at = fresh, now
            if codex is None or now - codex_at >= CODEX_EVERY:
                fresh = fetch_codex()
                if fresh is not None:
                    codex, codex_at = fresh, now
            if claude is None:
                errors.append("claude-swap не отвечает (cswap list --json)")
            if codex is None:
                errors.append("CodexBar не отвечает (codexbar usage)")

            frame = render(
                claude,
                codex,
                time.time() - claude_at if claude else None,
                time.time() - codex_at if codex else None,
                errors,
            )
            if once:
                print(frame)
                return 0
            sys.stdout.write("\033[H\033[2J" + frame + "\n")
            sys.stdout.flush()
            time.sleep(10)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
