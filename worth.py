"""What a share of a Claude limit is worth: local token usage priced at API rates.

Transcripts carry no account. Claude Desktop files each of its Code sessions under
the account it is signed in to; every other session runs as the account cswap last
switched Claude Code to, so cswap's log is the account history. claude.ai, the phone
and other machines spend the same limits unseen, so the estimates run low.
"""

import json
import os
import re
import sqlite3
import time
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path

from metadata import parse_date

# Windows reach a week back; one more day keeps a window whose reset just passed.
KEEP = timedelta(days=8).total_seconds()
SPAN = {"5h": timedelta(hours=5)}  # every other window is weekly
WEEK = timedelta(days=7)
# Below this the integer percentage alone can move an estimate by more than 5%.
ROUGH_BELOW = 10
TOKENS = ("input", "output", "cache_read", "cache_write")

# $ per million tokens: input, output, cache read, 5-minute and 1-hour cache writes.
# The first matching name wins; a release not listed yet gets its family's latest rates.
FABLE_51 = (10, 50, 0.25, 12.5, 20)
OPUS_55 = (4, 20, 0.2, 5, 8)
PRICES = (
    ("fable-5-1", FABLE_51), ("mythos-5-1", FABLE_51),
    ("fable-5", (10, 50, 1, 12.5, 20)), ("mythos-5", (10, 50, 1, 12.5, 20)),
    ("opus-5-5", OPUS_55), ("opus-5", (5, 25, 0.5, 6.25, 10)), ("opus-4", (5, 25, 0.5, 6.25, 10)),
    ("sonnet-4", (3, 15, 0.3, 3.75, 6)), ("sonnet", (2, 10, 0.2, 2.5, 4)),
    ("haiku", (1, 5, 0.1, 1.25, 2)),
    ("fable", FABLE_51), ("mythos", FABLE_51), ("opus", OPUS_55),
)

SCHEMA = 1
TABLES = """
CREATE TABLE files (path TEXT PRIMARY KEY, offset INTEGER NOT NULL);
CREATE TABLE replies (key TEXT PRIMARY KEY, ts REAL NOT NULL, model TEXT NOT NULL, session TEXT,
                      desktop INTEGER NOT NULL, input INTEGER NOT NULL, output INTEGER NOT NULL,
                      cache_read INTEGER NOT NULL, write_5m INTEGER NOT NULL, write_1h INTEGER NOT NULL,
                      fast INTEGER NOT NULL);
CREATE INDEX replies_ts ON replies (ts);
"""
# A reply is written once per content block while it streams; the last copy has all its output.
UPSERT = ("INSERT INTO replies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
          "ON CONFLICT (key) DO UPDATE SET output = max(output, excluded.output)")
LOG_LINE = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ - \w+ - (.*)")


def cache_dir():
    return Path(os.environ.get("XDG_CACHE_HOME") or "~/.cache").expanduser() / "limits"


def projects_dir():
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(config).expanduser() if config else Path.home() / ".claude") / "projects"


def price(model, counts, fast=False):
    """Counts are input, output, cache read, 5-minute and 1-hour cache writes; fast mode costs double."""
    rates = next((rates for name, rates in PRICES if name in model), (0, 0, 0, 0, 0))
    return sum(n * rate for n, rate in zip(counts, rates)) / 1e6 * (2 if fast else 1)


def count(value):
    return value if isinstance(value, int) and value > 0 else 0


def parse(line, since):
    """The index row for an assistant reply in one transcript line, else None."""
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, dict) or entry.get("type") != "assistant" or not isinstance(entry.get("message"), dict):
        return None
    message = entry["message"]
    usage, model, moment = message.get("usage"), message.get("model"), parse_date(entry.get("timestamp"))
    if not isinstance(usage, dict) or not isinstance(model, str) or model.startswith("<") or not moment:
        return None
    if moment.timestamp() < since:
        return None
    written = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    five, hour = count(written.get("ephemeral_5m_input_tokens")), count(written.get("ephemeral_1h_input_tokens"))
    if "ephemeral_5m_input_tokens" not in written and "ephemeral_1h_input_tokens" not in written:
        hour = count(usage.get("cache_creation_input_tokens"))  # no split: Claude Code caches for an hour
    key = f"{message['id']}:{entry.get('requestId')}" if message.get("id") else entry.get("uuid")
    if not isinstance(key, str):
        return None
    return (key, moment.timestamp(), model, entry.get("sessionId"), entry.get("entrypoint") == "claude-desktop",
            count(usage.get("input_tokens")), count(usage.get("output_tokens")),
            count(usage.get("cache_read_input_tokens")), five, hour, usage.get("speed") == "fast")


def open_index(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))  # private before SQLite writes a byte
    db = sqlite3.connect(str(path), timeout=30)
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA:
        db.executescript("DROP TABLE IF EXISTS files; DROP TABLE IF EXISTS replies;"
                         + TABLES + f"PRAGMA user_version = {SCHEMA};")
    return db


def read_new(db, path, offset, since):
    """Index the complete lines after offset; returns the offset past the last of them."""
    rows = []
    try:
        with open(path, "rb") as stream:
            stream.seek(offset)
            for line in stream:
                if not line.endswith(b"\n"):
                    break  # still being written: the next pass reads it whole
                offset += len(line)
                row = parse(line, since) if b'"assistant"' in line and b'"usage"' in line else None
                if row:
                    rows.append(row)
    except OSError:
        pass
    db.executemany(UPSERT, rows)
    return offset


def update_index(db, root, now):
    """Read transcript lines written since the last pass and forget replies older than KEEP."""
    since = now - KEEP
    left = dict(db.execute("SELECT path, offset FROM files"))
    for directory, _, names in os.walk(root):
        for name in names:
            path = os.path.join(directory, name)
            try:
                info = os.stat(path) if name.endswith(".jsonl") else None
            except OSError:
                continue
            if info is None or info.st_mtime < since:
                continue
            done = left.pop(path, 0)
            if info.st_size == done:
                continue
            # A file shorter than what was read has been rewritten: read it again.
            done = read_new(db, path, done if info.st_size > done else 0, since)
            db.execute("INSERT OR REPLACE INTO files VALUES (?, ?)", (path, done))
            db.commit()
    db.executemany("DELETE FROM files WHERE path = ?", [(path,) for path in left])
    db.execute("DELETE FROM replies WHERE ts < ?", (since,))
    db.commit()


def account_history(directory):
    """(time, email or None) whenever cswap's log shows the account Claude Code runs as.

    None marks a credential cswap could not place. The log is in local time and
    names slots, so the slot of each email is followed through the same log.
    """
    lines = []
    for name in ("claude-swap.log.3", "claude-swap.log.2", "claude-swap.log.1", "claude-swap.log"):
        try:
            lines += (Path(directory) / name).read_text(errors="replace").splitlines()
        except OSError:
            pass
    slots, history = {}, []
    for line in lines:
        match = LOG_LINE.match(line)
        if not match:
            continue
        moment, text = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S").timestamp(), match[2]
        found = re.match(r"(?:Added|Updated credentials for) account (\d+): (\S+)", text)
        if found:  # cswap stores the account Claude Code is signed in to
            slots[found[1]] = found[2]
            history.append((moment, found[2]))
            continue
        found = re.match(r"(?:Switched from account \d+ to|Resynced account) (\d+)", text)
        if found:
            history.append((moment, slots.get(found[1])))
            continue
        if re.match(r"Live credential (resolves to a different account|does not belong)", text):
            history.append((moment, None))
            continue
        found = re.match(r"Added account (\d+) from .*: (\S+)$", text)
        if found:
            slots[found[1]] = found[2]
        found = re.match(r"Moved slot: (\d+) \((\S+)\) -> (\d+)", text)
        if found:
            slots.pop(found[1], None)
            slots[found[3]] = found[2]
        found = re.match(r"Swapped slots: (\d+) \((\S+)\) <-> (\d+) \((\S+)\)", text)
        if found:
            slots[found[1]], slots[found[3]] = found[4], found[2]
        found = re.match(r"Removed account (\d+):", text)
        if found:
            slots.pop(found[1], None)
    return history


def desktop_accounts(directory, orgs, since):
    """CLI session id -> email for Claude Desktop's Code sessions, which run as Desktop's account.

    Desktop keeps them as <user>/<organization>/local_*.json. A session of an
    organization cswap does not know maps to "", so it counts for no account.
    """
    found = {}
    for path in Path(directory).glob("*/*/local_*.json"):
        try:
            if path.stat().st_mtime < since:
                continue
            session = json.loads(path.read_text()).get("cliSessionId")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(session, str):
            found[session] = orgs.get(path.parent.name, "")
    return found


def window_end(window):
    """When the window resets, to the minute: cswap's reset time drifts by a second between reads."""
    resets = parse_date(window.get("resets_at"))
    return round(resets.timestamp() / 60) * 60 if resets else None


def window_start(window):
    resets = parse_date(window.get("resets_at"))
    return (resets - SPAN.get(window["label"], WEEK)).timestamp() if resets else None


def window_spend(accounts, now=None):
    """API price and tokens of local usage in each Claude window, by the account it ran as.

    {(email, label): {"start", "usd", "tokens", "models", "unattributed_usd"}}, where models
    splits usd and tokens by model. Usage nobody can be placed with counts as unattributed
    in every window it falls into.
    """
    now = now or time.time()
    windows = [(acc["email"], w, window_start(w)) for acc in accounts for w in acc["windows"]]
    windows = [(email, w, start) for email, w, start in windows if start is not None]
    if not windows:
        return {}
    db = open_index(cache_dir() / "usage.sqlite3")
    try:
        update_index(db, projects_dir(), now)
        rows = db.execute("SELECT ts, model, session, desktop, input, output, cache_read, write_5m, write_1h, fast "
                          "FROM replies WHERE ts >= ?", (min(start for *_, start in windows),)).fetchall()
    finally:
        db.close()
    orgs = {acc["org"]: acc["email"] for acc in accounts if acc.get("org")}
    desktop = desktop_accounts(Path.home() / "Library/Application Support/Claude/claude-code-sessions",
                               orgs, now - KEEP)
    history = account_history(Path.home() / ".claude-swap-backup")
    moments = [moment for moment, _ in history]
    result, mine = {}, {}
    for email, w, start in windows:
        spent = dict(bucket(), start=start, models={}, unattributed_usd=0.0)
        result[(email, w["label"])] = spent
        mine.setdefault(email, []).append((start, (w.get("scope") or "").casefold(), spent))
    everyone = [window for group in mine.values() for window in group]
    for ts, model, session, from_desktop, *counts, fast in rows:
        owner = desktop.get(session) if from_desktop else None
        if owner is None:
            # Desktop's own sessions have a record; what they launch runs as Claude Code does.
            at = bisect_right(moments, ts) - 1
            owner = history[at][1] if at >= 0 else None
        usd, family = price(model, counts, fast), model.casefold()
        for start, scope, spent in mine.get(owner, ()) if owner is not None else everyone:
            if ts < start or scope not in family:
                continue
            if owner is None:
                spent["unattributed_usd"] += usd
                continue
            add(spent, usd, counts)
            add(spent["models"].setdefault(model, bucket()), usd, counts)
    return result


def bucket():
    return {"usd": 0.0, "tokens": dict.fromkeys(TOKENS, 0)}


def add(into, usd, counts):
    into["usd"] += usd
    tokens = into["tokens"]
    tokens["input"] += counts[0]
    tokens["output"] += counts[1]
    tokens["cache_read"] += counts[2]
    tokens["cache_write"] += counts[3] + counts[4]


def lookup(spend, email, window):
    """What was spent in this very window; None after it has started over."""
    spent = (spend or {}).get((email, window["label"]))
    start = window_start(window)
    return spent if spent and start is not None and abs(spent["start"] - start) < 600 else None


def estimate(spent, pct):
    """The price and tokens of 1% of a window, or None while there is nothing to divide."""
    if not spent or not pct or pct < 1 or spent["usd"] <= 0:
        return None
    return {"usd": spent["usd"] / pct, "tokens": sum(spent["tokens"].values()) / pct, "rough": pct < ROUGH_BELOW}


def summary(spent, pct):
    """JSON form of a window's spend, split by model, and of 1% and 100% of it."""
    if not spent:
        return None
    one = estimate(spent, pct)
    return {
        "spent_usd": round(spent["usd"], 2), "tokens": spent["tokens"],
        "models": {model: {"usd": round(part["usd"], 2), "tokens": part["tokens"]}
                   for model, part in sorted(spent["models"].items(), key=lambda item: -item[1]["usd"])},
        "unattributed_usd": round(spent["unattributed_usd"], 2),
        "per_percent": one and {"usd": round(one["usd"], 4), "tokens": round(one["tokens"])},
        "full": one and {"usd": round(one["usd"] * 100, 2), "tokens": round(one["tokens"] * 100)},
        "rough": bool(one and one["rough"]),
    }


def history_path():
    data = os.environ.get("XDG_DATA_HOME")
    return (Path(data).expanduser() if data else Path.home() / ".local/share") / "limits/history.jsonl"


# Last percentage written per window, so a window gets a new line only when it moves on.
_written = {}


def written(path):
    """The last percentage in the history per window; read once per process from the file's tail."""
    if path not in _written:
        last = _written[path] = {}
        try:
            with open(path, "rb") as stream:
                stream.seek(max(0, stream.seek(0, os.SEEK_END) - (4 << 20)))
                tail = stream.read().splitlines()[1:]
        except OSError:
            tail = []
        for line in tail:
            try:
                entry = json.loads(line)
                last[(entry["email"], entry["window"], entry["resets_at"])] = entry["pct"]
            except (ValueError, KeyError, TypeError):
                continue
    return _written[path]


def record_history(accounts, spend, now=None):
    """Append what each Claude window was worth whenever its percentage has moved on.

    One line per step: 1 point for weekly windows, 5 for the 5-hour one. The lines of
    a window trace its course; its last line is the most exact estimate. Kept in
    ~/.local/share/limits/history.jsonl, outside the cache, to compare windows over time.
    """
    path = history_path()
    last, lines = written(path), []
    moment = datetime.fromtimestamp(now or time.time(), timezone.utc).isoformat(timespec="seconds")
    for acc in accounts:
        for w in acc["windows"]:
            spent, end = lookup(spend, acc["email"], w), window_end(w)
            if end is None or not estimate(spent, w["pct"]):
                continue
            resets = datetime.fromtimestamp(end, timezone.utc).isoformat()
            key, step = (acc["email"], w["label"], resets), 5 if w["label"] == "5h" else 1
            if key in last and w["pct"] < min(100, last[key] + step):
                continue
            last[key] = w["pct"]
            lines.append(json.dumps({
                "at": moment, "email": acc["email"], "plan": acc.get("plan") or "", "window": w["label"],
                "scope": w.get("scope"), "resets_at": resets, "pct": w["pct"], **summary(spent, w["pct"]),
            }, ensure_ascii=False) + "\n")
    if lines:
        path.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a") as stream:
            stream.write("".join(lines))
    return len(lines)


def load_history(path=None):
    """The final line of every window in the history: its highest percentage, first reached."""
    final = {}
    try:
        with open(path or history_path(), "rb") as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                    key = (entry["email"], entry["window"], entry["resets_at"])
                    if key not in final or entry["pct"] > final[key]["pct"]:
                        final[key] = entry
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return sorted(final.values(), key=lambda entry: entry["resets_at"])
