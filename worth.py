"""What a share of a Claude limit is worth: local token usage priced at API rates.

Facts are stored and estimates are derived from them. Percent readings, plans and
Claude Desktop session links are appended to observations.jsonl as they are seen:
nothing can bring them back later. Replies and limit hits are indexed from the
transcripts and can be rebuilt. A window splits into segments at its reset and
wherever its percentage starts over (a plan change, a reset by Anthropic); a
segment is measured by the moment the limit was hit, by the difference between
two readings, or by the level reached since its start.

Transcripts carry no account. Claude Desktop files each of its Code sessions under
the account it is signed in to; every other session runs as the account cswap last
switched Claude Code to, so cswap's log is the account history. claude.ai, the phone
and other machines spend the same limits unseen, so the estimates run low.
"""

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import time
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from metadata import claude_profiles, parse_date

KEEP = timedelta(days=90).total_seconds()
WEEK = 7 * 86400
SPAN = {"5h": 5 * 3600}  # every other window is weekly
LABEL = {"five_hour": "5h", "seven_day": "7d"}
EDGE = 60  # seconds after a switch in which a running session may still be on the old account
# Spend after a hit and before the reset shows the window started over, when it is more
# than the tail of requests that were already under way: this many dollars and this share
# of what the window had cost by the hit.
RESUMED_USD, RESUMED_SHARE = 20.0, 0.1
OPENED_LATE = 660  # a 5h window opens with its first request, rounded down to ten minutes
MIN_BASELINE = 20  # points between two readings before their difference is trusted
FIT_DAYS, FIT_MIN, FIT_R2 = 30, 500, 0.9

# $ per million tokens: input, output, cache read, 5-minute and 1-hour cache writes
# (API prices of 2026-09-25). The first matching name wins; an unknown model has no price.
PRICES = (
    ("fable-5-1", (10, 50, 0.25, 12.5, 20)), ("mythos-5-1", (10, 50, 0.25, 12.5, 20)),
    ("fable-5", (10, 50, 1, 12.5, 20)), ("mythos-5", (10, 50, 1, 12.5, 20)),
    ("opus-5-5", (4, 20, 0.2, 5, 8)), ("opus-5", (5, 25, 0.5, 6.25, 10)), ("opus-4", (5, 25, 0.5, 6.25, 10)),
    ("sonnet-5", (2, 10, 0.2, 2.5, 4)), ("sonnet-4", (3, 15, 0.3, 3.75, 6)),
    ("haiku-4", (1, 5, 0.1, 1.25, 2)),
)

SCHEMA = 3
TABLES = (
    "CREATE TABLE files (path TEXT PRIMARY KEY, inode INTEGER NOT NULL, offset INTEGER NOT NULL)",
    "CREATE TABLE replies (key INTEGER PRIMARY KEY, ts REAL NOT NULL, model TEXT NOT NULL, session INTEGER, "
    "desktop INTEGER NOT NULL, sub INTEGER NOT NULL, input INTEGER NOT NULL, output INTEGER NOT NULL, "
    "cache_read INTEGER NOT NULL, write_5m INTEGER NOT NULL, write_1h INTEGER NOT NULL, fast INTEGER NOT NULL, "
    "final INTEGER NOT NULL, seen INTEGER NOT NULL, text_len INTEGER NOT NULL, args_len INTEGER NOT NULL, "
    "sig_len INTEGER NOT NULL)",
    "CREATE INDEX replies_ts ON replies (ts)",
    "CREATE TABLE hits (key INTEGER PRIMARY KEY, ts REAL NOT NULL, session INTEGER, desktop INTEGER NOT NULL, "
    "kind TEXT NOT NULL, resets REAL NOT NULL)",
)
# A reply is written once per content block while it streams. The copy written when it
# ends has a stop reason, all its output and the speed it ran at; each copy adds the
# length of its own block once (`seen` is a bit per block), so reading a file again is harmless.
UPSERT = (
    "INSERT INTO replies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET "
    "ts = min(ts, excluded.ts), output = max(output, excluded.output), fast = max(fast, excluded.fast), "
    "final = max(final, excluded.final), seen = seen | excluded.seen, "
    + ", ".join(f"{c} = {c} + CASE WHEN seen & excluded.seen THEN 0 ELSE excluded.{c} END"
                for c in ("text_len", "args_len", "sig_len")))
LOG_LINE = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ - \w+ - (.*)")
HIT_TEXT = re.compile(r"You've hit your (session|weekly) limit · resets (?:(\w{3}) (\d{1,2}) at )?"
                      r"(\d{1,2})(?::(\d\d))?(am|pm) \(([^)]+)\)")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def cache_dir():
    return Path(os.environ.get("XDG_CACHE_HOME") or "~/.cache").expanduser() / "limits"


def data_dir():
    data = os.environ.get("XDG_DATA_HOME")
    return (Path(data).expanduser() if data else Path.home() / ".local/share") / "limits"


def projects_dir():
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(config).expanduser() if config else Path.home() / ".claude") / "projects"


def private(path):
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def rates(model):
    return next((rates for name, rates in PRICES if name in model), None)


def count(value):
    return value if isinstance(value, int) and value > 0 else 0


def h64(text):
    return int.from_bytes(hashlib.blake2b(text.encode(), digest_size=8).digest(), "big", signed=True)


def minute(ts):
    """cswap's reset time drifts by a second between reads; a window is told by its minute."""
    return round(ts / 60) * 60


# The index: replies and limit hits from the transcripts.

def reset_in_text(text, moment):
    """(kind, reset time) from "You've hit your weekly limit · resets Sep 30 at 10pm (Europe/Moscow)"."""
    found = HIT_TEXT.search(text)
    if not found:
        return None
    kind, month, day, hour, minutes, half, zone = found.groups()
    try:
        local = moment.astimezone(ZoneInfo(zone))
    except Exception:
        return None
    resets = local.replace(hour=int(hour) % 12 + (12 if half == "pm" else 0), minute=int(minutes or 0),
                           second=0, microsecond=0)
    if month in MONTHS:
        resets = resets.replace(month=MONTHS.index(month) + 1, day=int(day))
        if resets < local - timedelta(days=30):
            resets = resets.replace(year=resets.year + 1)
    elif resets <= local:
        resets += timedelta(days=1)
    return {"session": "five_hour", "weekly": "seven_day"}[kind], resets.timestamp()


def parse(line, since, sub=False):
    """("reply", row) or ("hit", row) for an assistant line of a transcript, else None."""
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, dict) or entry.get("type") != "assistant" or not isinstance(entry.get("message"), dict):
        return None
    message, moment = entry["message"], parse_date(entry.get("timestamp"))
    usage, model = message.get("usage"), message.get("model")
    if not isinstance(model, str) or not moment or moment.timestamp() < since:
        return None
    blocks = message.get("content") if isinstance(message.get("content"), list) else []
    block = blocks[0] if blocks and isinstance(blocks[0], dict) else {}
    session = h64(entry["sessionId"]) if isinstance(entry.get("sessionId"), str) else None
    desktop = entry.get("entrypoint") == "claude-desktop"
    if entry.get("error") == "rate_limit" and isinstance(entry.get("uuid"), str):
        quota = entry.get("quotaLimits") if isinstance(entry.get("quotaLimits"), dict) else {}
        hit = ((quota.get("rateLimitType"), quota.get("resetsAt")) if quota.get("resetsAt")
               else reset_in_text(str(block.get("text") or ""), moment))
        if not hit or not isinstance(hit[0], str) or not isinstance(hit[1], (int, float)):
            return None
        return "hit", (h64(entry["uuid"]), moment.timestamp(), session, desktop, hit[0], float(hit[1]))
    if not isinstance(usage, dict) or model.startswith("<"):
        return None
    written = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    five, hour = count(written.get("ephemeral_5m_input_tokens")), count(written.get("ephemeral_1h_input_tokens"))
    if "ephemeral_5m_input_tokens" not in written and "ephemeral_1h_input_tokens" not in written:
        five = count(usage.get("cache_creation_input_tokens"))  # no split: the API's default 5-minute cache
    key = f"{message['id']}:{entry.get('requestId')}" if message.get("id") else entry.get("uuid")
    if not isinstance(key, str):
        return None
    index = entry.get("apiBlockIndex")
    kind = block.get("type")
    return "reply", (
        h64(key), moment.timestamp(), model, session, desktop, sub,
        count(usage.get("input_tokens")), count(usage.get("output_tokens")),
        count(usage.get("cache_read_input_tokens")), five, hour, usage.get("speed") == "fast",
        message.get("stop_reason") is not None, 1 << min(index if isinstance(index, int) and index >= 0 else 0, 62),
        len(block.get("text") or "") if kind == "text" else 0,
        len(json.dumps(block.get("input"))) if kind == "tool_use" else 0,
        len(block.get("signature") or "") if kind == "thinking" else 0)


def open_index(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))  # private before SQLite writes a byte
    db = sqlite3.connect(str(path), timeout=30)
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    for name in (path, f"{path}-wal", f"{path}-shm"):
        private(name)
    # Two processes may meet a missing or older index at once: settle it in one write transaction.
    db.execute("BEGIN IMMEDIATE")
    if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA:
        for table in ("files", "replies", "hits", "messages"):
            db.execute(f"DROP TABLE IF EXISTS {table}")
        for statement in TABLES:
            db.execute(statement)
        db.execute(f"PRAGMA user_version = {SCHEMA}")
    db.commit()
    return db


def read_new(db, path, offset, since):
    """Index the complete lines after offset; returns the offset past the last of them."""
    replies, hits, sub = [], [], "/subagents/" in path
    try:
        with open(path, "rb") as stream:
            stream.seek(offset)
            for line in stream:
                if not line.endswith(b"\n"):
                    break  # still being written: the next pass reads it whole
                offset += len(line)
                found = parse(line, since, sub) if b'"assistant"' in line and b'"usage"' in line else None
                if found:
                    (replies if found[0] == "reply" else hits).append(found[1])
    except OSError:
        pass
    db.executemany(UPSERT, replies)
    db.executemany("INSERT OR IGNORE INTO hits VALUES (?, ?, ?, ?, ?, ?)", hits)
    return offset


def update_index(db, root, now):
    """Read transcript lines written since the last pass and forget what is older than KEEP."""
    since = now - KEEP
    left = {path: (inode, offset) for path, inode, offset in db.execute("SELECT path, inode, offset FROM files")}
    for directory, _, names in os.walk(root):
        for name in names:
            path = os.path.join(directory, name)
            try:
                info = os.stat(path) if name.endswith(".jsonl") else None
            except OSError:
                continue
            if info is None or info.st_mtime < since:
                continue
            inode, done = left.pop(path, (info.st_ino, 0))
            if inode != info.st_ino or info.st_size < done:
                done = 0  # replaced or rewritten: read it again from the start
            if info.st_size == done:
                continue
            done = read_new(db, path, done, since)
            db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?)", (path, info.st_ino, done))
            db.commit()
    db.executemany("DELETE FROM files WHERE path = ?", [(path,) for path in left])
    db.execute("DELETE FROM replies WHERE ts < ?", (since,))
    db.execute("DELETE FROM hits WHERE ts < ?", (since,))
    db.commit()


# Who ran what: cswap's log and Claude Desktop's session records.

def account_history(directory):
    """(time, email or None) whenever cswap's log shows the account Claude Code runs as, by time.

    None marks a credential cswap could not place, from the moment it was written when the
    log says so. The log is in local time and names slots, so the slot of each email is
    followed through the same log.
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
        found = re.match(r"(?:Switched from account \d+ to|Resynced account|Activated account) (\d+)", text)
        if found:
            history.append((moment, slots.get(found[1])))
            continue
        if re.match(r"Live credential (resolves to a different account|does not belong)", text):
            written = parse_date((re.search(r"credentials mtime (\S+?)\)", text) or [None, None])[1])
            history.append((min(moment, written.timestamp()) if written else moment, None))
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
    return sorted(history, key=lambda event: event[0])


_records = {}  # Desktop session record path -> (mtime, CLI session id)


def desktop_sessions(directory, orgs):
    """CLI session id -> email for the Code sessions Claude Desktop has on record now.

    Desktop keeps them as <user>/<organization>/local_*.json and runs them as its own
    account. A session of an organization cswap does not manage maps to "".
    """
    found = {}
    for path in Path(directory).glob("*/*/local_*.json"):
        try:
            mtime = path.stat().st_mtime
            if _records.get(path, (None,))[0] != mtime:
                _records[path] = (mtime, json.loads(path.read_text()).get("cliSessionId"))
        except (OSError, ValueError, AttributeError):
            continue
        session = _records[path][1]
        if isinstance(session, str):
            found[session] = orgs.get(path.parent.name, "")
    return found


# Observations: what cannot be read again later.

def observations_path():
    return data_dir() / "observations.jsonl"


def shape(windows):
    """Windows of a reading as stored: label -> [percent, reset minute as ISO or None]."""
    result = {}
    for w in windows:
        resets = parse_date(w.get("resets_at"))
        end = datetime.fromtimestamp(minute(resets.timestamp()), timezone.utc).isoformat() if resets else None
        result[w["label"]] = [w["pct"], end]
    return result


_seen = {}  # observations path -> what the file holds so far and how far it has been read


def catch_up(stream, path):
    """The state of the observations file, including lines other processes have added since."""
    state = _seen.setdefault(path, {"offset": 0, "pct": {}, "plan": {}, "sessions": set(), "held": {}})
    size = stream.seek(0, os.SEEK_END)
    if state["offset"] > size:  # replaced by a shorter file: start over
        state.update(offset=0, pct={}, plan={}, sessions=set(), held={})
    stream.seek(state["offset"])
    for line in stream.read().split(b"\n"):
        try:
            entry = json.loads(line)
            if entry["t"] == "pct":
                state["pct"][entry["email"]] = (entry["at"], entry["w"])
            elif entry["t"] == "plan":
                state["plan"][entry["email"]] = entry["plan"]
            elif entry["t"] == "session":
                state["sessions"].add(entry["session"])
        except (ValueError, KeyError, TypeError):
            continue
    state["offset"] = size
    return state


def observe(accounts, now=None):
    """Append what the Claude accounts show now and nothing will show again: returns lines written.

    A reading is written when cswap has read the usage anew and a percentage or a reset has
    changed; the last reading of the old values is written just before it, so the time of
    the change is bracketed. A plan is taken from the live profile of the active account
    (a saved copy lags behind a plan change) and from saved copies only until something
    better is known. Desktop session links are written once, before Desktop forgets them.
    """
    path = observations_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    moment = datetime.fromtimestamp(now or time.time(), timezone.utc).isoformat(timespec="seconds")
    with os.fdopen(os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600), "a+b") as stream:
        private(path)
        fcntl.flock(stream, fcntl.LOCK_EX)
        state, lines = catch_up(stream, path), []
        if not state["offset"]:
            lines += earlier_history(path.parent / "history.jsonl")
        for acc in accounts:
            at, w = acc.get("fetched_at"), shape(acc["windows"])
            last = state["pct"].get(acc["email"])
            if not w or not parse_date(at) or (last and parse_date(last[0]) >= parse_date(at)):
                continue
            line = {"v": 1, "t": "pct", "at": at, "email": acc["email"], "active": bool(acc.get("active")), "w": w}
            if last and last[1] == w:
                state["held"][acc["email"]] = line
                continue
            lines += [held for held in [state["held"].pop(acc["email"], None)] if held]
            lines.append(line)
            state["pct"][acc["email"]] = (at, w)
        managed = {acc["email"] for acc in accounts}
        for profile in claude_profiles():
            email, plan = profile["email"], profile["plan"]
            if email not in managed or state["plan"].get(email) == plan or not (profile["live"] or email not in state["plan"]):
                continue
            fetched = profile["fetched_at"] if not profile["live"] else None
            lines.append({"v": 1, "t": "plan", "at": fetched or moment, "email": email, "plan": plan,
                          "src": "live" if profile["live"] else "saved"})
            state["plan"][email] = plan
        orgs = {acc["org"]: acc["email"] for acc in accounts if acc.get("org")}
        records = desktop_sessions(Path.home() / "Library/Application Support/Claude/claude-code-sessions", orgs)
        for session, email in sorted(records.items()):
            if session not in state["sessions"]:
                lines.append({"v": 1, "t": "session", "at": moment, "session": session, "email": email})
                state["sessions"].add(session)
        if lines:
            stream.write(b"".join(json.dumps(line, ensure_ascii=False).encode() + b"\n" for line in lines))
            stream.flush()
            state["offset"] = stream.seek(0, os.SEEK_END)
    return len(lines)


def earlier_history(path):
    """Readings kept by the first version of the history, which stored finished estimates."""
    readings = {}
    try:
        for line in path.read_text().splitlines():
            entry = json.loads(line)
            reading = readings.setdefault((entry["measured_until"], entry["email"]), {})
            reading[entry["window"]] = [entry["pct"], entry["resets_at"]]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return [{"v": 1, "t": "pct", "at": at, "email": email, "src": "history", "w": w}
            for (at, email), w in sorted(readings.items())]


def load_observations(path=None):
    """readings {(email, label): [(time, percent, reset or None)]}, plans {email: [(time, plan)]}, sessions."""
    readings, plans, sessions = {}, {}, {}
    try:
        with open(path or observations_path(), "rb") as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                    at = parse_date(entry["at"]).timestamp()
                    if entry["t"] == "pct":
                        for label, (pct, end) in entry["w"].items():
                            end = parse_date(end)
                            readings.setdefault((entry["email"], label), []).append(
                                (at, float(pct), end.timestamp() if end else None))
                    elif entry["t"] == "plan":
                        plans.setdefault(entry["email"], []).append((at, entry["plan"]))
                    elif entry["t"] == "session":
                        sessions[entry["session"]] = entry["email"]
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
    except OSError:
        pass
    for series in list(readings.values()) + list(plans.values()):
        series.sort()
    return readings, plans, sessions


# Measuring.

def solve(matrix, vector):
    """Gaussian elimination with pivoting; None when the system has no single answer."""
    n = len(vector)
    rows = [list(row) + [value] for row, value in zip(matrix, vector)]
    for i in range(n):
        pivot = max(range(i, n), key=lambda r: abs(rows[r][i]))
        if abs(rows[pivot][i]) < 1e-9:
            return None
        rows[i], rows[pivot] = rows[pivot], rows[i]
        for r in range(n):
            if r != i:
                factor = rows[r][i] / rows[i][i]
                rows[r] = [a - factor * b for a, b in zip(rows[r], rows[i])]
    return [rows[i][n] / rows[i][i] for i in range(n)]


def output_models(db, now):
    """Per model, how a reply's output follows from its content: {model: (b0, text, args, signature, r2, n)}.

    Subagent transcripts often lack the copy written when a reply ends, so only the first
    streamed output count is known while the content itself is all there. The fit is made
    on ended subagent replies of the same model; with too few of them or a poor fit the
    model's mean output stands in (r2 is None then).
    """
    cols = ("1", "text_len", "args_len", "sig_len")
    pairs = [(a, b) for i, a in enumerate(cols) for b in cols[i:]]
    moments = ", ".join(f"SUM(1.0 * {a} * {b})" for a, b in pairs)
    targets = ", ".join(f"SUM(1.0 * {c} * output)" for c in cols)
    query = (f"SELECT model, COUNT(*), SUM(1.0 * output * output), {targets}, {moments} FROM replies "
             "WHERE final = 1 AND fast = 0 AND ts >= ? AND sub >= ? GROUP BY model")
    since, result = now - FIT_DAYS * 86400, {}
    means = {row[0]: (row[3] / row[1], row[1]) for row in db.execute(query, (now - KEEP, 0))}
    for model, n, yy, *sums in db.execute(query, (since, 1)):
        xy, flat = sums[:4], sums[4:]
        xx = [[0.0] * 4 for _ in range(4)]
        for (a, b), value in zip(pairs, flat):
            xx[cols.index(a)][cols.index(b)] = xx[cols.index(b)][cols.index(a)] = value
        # A kind of content these replies never have leaves a zero row; it gets no coefficient.
        used = [i for i in range(4) if xx[i][i] > 0]
        part = solve([[xx[i][j] for j in used] for i in used], [xy[i] for i in used]) if n >= FIT_MIN else None
        beta = part and [part[used.index(i)] if i in used else 0.0 for i in range(4)]
        if beta:
            total = yy - xy[0] ** 2 / n
            error = yy - 2 * sum(b * v for b, v in zip(beta, xy)) + sum(
                beta[i] * beta[j] * xx[i][j] for i in range(4) for j in range(4))
            r2 = 1 - error / total if total > 0 else 0.0
            if r2 >= FIT_R2:
                result[model] = (*beta, r2, n)
    for model, (mean, n) in means.items():
        result.setdefault(model, (mean, 0.0, 0.0, 0.0, None, n))
    return result


def prepare(db, history, links, fits):
    """A temporary table `spend`: every reply with its owner, its price by part and what is uncertain in it."""
    db.execute("CREATE TEMP TABLE spans (t0 REAL PRIMARY KEY, owner TEXT)")
    owner = object()
    for moment, email in history:
        if email != owner:  # only changes start a span
            db.execute("INSERT OR REPLACE INTO spans VALUES (?, ?)", (moment, email))
            owner = email
    db.execute("CREATE TEMP TABLE links (session INTEGER PRIMARY KEY, owner TEXT)")
    db.executemany("INSERT OR REPLACE INTO links VALUES (?, ?)", [(h64(s), e) for s, e in links.items()])
    db.execute("CREATE TEMP TABLE rates (model TEXT PRIMARY KEY, i REAL, o REAL, r REAL, w5 REAL, w1 REAL, "
               "b0 REAL, b1 REAL, b2 REAL, b3 REAL)")
    unpriced = {}
    for model, n in db.execute("SELECT model, COUNT(*) FROM replies GROUP BY model").fetchall():
        if rates(model):
            db.execute("INSERT INTO rates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (model, *[rate / 1e6 for rate in rates(model)], *fits.get(model, (0, 0, 0, 0))[:4]))
        else:
            unpriced[model] = n
    # Desktop's own sessions have a link; whatever else a Desktop session launches is taken
    # to run as Claude Code does (`alone`), which the limit hits keep checking.
    db.execute("""
        CREATE TEMP TABLE spend AS
        SELECT ts, model, owner, usd_in + (output + guessed) * o + usd_read + usd_write AS usd, usd_read,
               t_in, output + guessed AS t_out, t_read, t_write, guessed * o AS usd_guessed, guessed, alone,
               CASE WHEN linked THEN 0 ELSE COALESCE(ts - t0 < :edge, 0) END AS edge
        FROM (SELECT r.ts AS ts, r.model AS model, p.o AS o, r.output AS output,
                     l.session IS NOT NULL AS linked, r.desktop AND l.session IS NULL AS alone,
                     COALESCE(l.owner, (SELECT s.owner FROM spans s WHERE s.t0 <= r.ts ORDER BY s.t0 DESC LIMIT 1)) AS owner,
                     (SELECT MAX(s.t0) FROM spans s WHERE s.t0 <= r.ts) AS t0,
                     r.input * p.i AS usd_in, r.cache_read * p.r AS usd_read,
                     r.write_5m * p.w5 + r.write_1h * p.w1 AS usd_write,
                     r.input AS t_in, r.cache_read AS t_read, r.write_5m + r.write_1h AS t_write,
                     CASE WHEN r.final THEN 0 ELSE MAX(0, CAST(
                         p.b0 + p.b1 * r.text_len + p.b2 * r.args_len + p.b3 * r.sig_len AS INTEGER) - r.output) END AS guessed
              FROM replies r JOIN rates p ON p.model = r.model
                   LEFT JOIN links l ON r.desktop AND l.session = r.session
              WHERE NOT r.fast)""", {"edge": EDGE})
    db.execute("CREATE INDEX temp.spend_owner ON spend (owner, ts)")
    return unpriced


PARTS = ("usd", "usd_read", "t_in", "t_out", "t_read", "t_write", "usd_guessed", "guessed")


def spent(db, owner, start, until, scope=""):
    """What the owner's replies in [start, until) cost, in all and by model; None is nobody's."""
    total = dict.fromkeys(PARTS + ("usd_alone", "usd_edge"), 0)
    total["models"] = {}
    query = (f"SELECT model, {', '.join(f'SUM({part})' for part in PARTS)}, SUM(usd * alone), SUM(usd * edge) "
             "FROM spend WHERE owner IS ? AND ts >= ? AND ts < ? GROUP BY model")
    for model, *sums in db.execute(query, (owner, start, until)):
        if scope not in model.casefold():
            continue
        for part, value in zip(PARTS + ("usd_alone", "usd_edge"), sums):
            total[part] += value or 0
        total["models"][model] = sums[0] or 0.0
    return total


def plan_for(plans, start, until):
    """The plan in force in a segment: seen inside it, else the last seen before, else the first after."""
    inside = [plan for at, plan in plans if start <= at <= until]
    before = [plan for at, plan in plans if at < start]
    # Right after a switch the live profile is still the saved copy, so the latest sighting counts.
    return inside[-1] if inside else before[-1] if before else plans[0][1] + "?" if plans else ""


def measure(accounts, now=None):
    """Every segment of every Claude window that can be measured, and what the numbers rest on.

    {"rows": [...], "data": {...}}. A row is one segment: who, which window, from when to
    when, what was spent and what 100% of it comes to, by one of three methods:
    «упор» (the limit was hit: 100% at that second), «разность» (spend between two
    readings over their difference) or «уровень» (spend since the segment began over
    the percentage). Ranges come from the integer percentage (±1 point) and from spend
    whose account is uncertain.
    """
    now = now or time.time()
    readings, plans, saved = load_observations()
    orgs = {acc["org"]: acc["email"] for acc in accounts if acc.get("org")}
    links = dict(saved, **desktop_sessions(Path.home() / "Library/Application Support/Claude/claude-code-sessions", orgs))
    history = account_history(Path.home() / ".claude-swap-backup")
    moments = [moment for moment, _ in history]
    db = open_index(cache_dir() / "usage.sqlite3")
    try:
        update_index(db, projects_dir(), now)
        fits = output_models(db, now)
        unpriced = prepare(db, history, links, fits)
        hits = db.execute("SELECT ts, session, desktop, kind, resets FROM hits ORDER BY ts").fetchall()
        linked = {h64(session): email for session, email in links.items()}

        # A weekly window resets at the same minute every week, a different one for each
        # account, so the reset named by a hit tells whose limit it was.
        phase, ends = {}, {}
        for (email, label), series in readings.items():
            for _, _, end in series:
                if end is not None:
                    ends[(label, minute(end))] = email
                    if label == "7d":
                        phase.setdefault(minute(end) % WEEK, set()).add(email)
        windows, checks = {}, {"matched": 0, "mismatched": [], "alone_matched": 0, "alone_mismatched": 0}
        for ts, session, desktop, kind, resets in hits:
            label, end = LABEL.get(kind, kind.replace("seven_day_", "").capitalize()), minute(resets)
            at = bisect_right(moments, ts) - 1
            alone = bool(desktop) and session not in linked
            guess = linked.get(session) if desktop and not alone else (history[at][1] if at >= 0 else None)
            if label == "5h":
                named = {ends[(label, end)]} if (label, end) in ends else set()
            else:
                named = phase.get(end % WEEK, set())
            owner = next(iter(named)) if len(named) == 1 else guess
            if len(named) == 1 and guess is not None:
                if guess == owner:
                    checks["matched"] += 1
                else:
                    checks["mismatched"].append({"at": ts, "by_log": guess, "by_reset": owner, "window": label})
                if alone:
                    checks["alone_matched" if guess == owner else "alone_mismatched"] += 1
            if owner:
                windows.setdefault((owner, label, end), {"hits": [], "readings": []})["hits"].append(ts)
        for (email, label), series in readings.items():
            for at, pct, end in series:
                if end is not None:
                    windows.setdefault((email, label, minute(end)), {"hits": [], "readings": []})["readings"].append((at, pct))

        def usd(owner, since, until, scope):
            return db.execute("SELECT SUM(usd) FROM spend WHERE owner IS ? AND ts >= ? AND ts < ? AND model LIKE ?",
                              (owner, since, until, f"%{scope}%")).fetchone()[0] or 0.0

        # Where an account's percentages started over (a plan change, a reset by Anthropic).
        # Seen two ways: a reading lower than the one before it in the same window, or spend
        # that went on in earnest after the limit had been hit and before its reset. Requests
        # already under way finish after a hit; such tails are small and end in more hits.
        log_from = moments[0] if moments else now
        restarts, dropped = {}, []
        for (email, label, end), seen in windows.items():
            scope = "" if label in ("5h", "7d") else label.casefold()
            start = end - SPAN.get(label, WEEK)
            dropped += [(email, last, at) for (last, before), (at, pct) in zip(seen["readings"], seen["readings"][1:])
                        if pct < before]
            found, since, blocked = [], start, min(seen["hits"], default=None)
            while blocked is not None:
                again = db.execute("SELECT MIN(ts) FROM spend WHERE owner IS ? AND ts > ? AND ts < ? AND model LIKE ?",
                                   (email, blocked + 300, min(end, now), f"%{scope}%")).fetchone()[0]
                if again is None:
                    break
                following = min((h for h in seen["hits"] if h > again), default=min(end, now))
                before = usd(email, since, blocked, scope)  # nothing: a hit the log pinned on the wrong account
                if before <= 0 or usd(email, again, following, scope) < max(RESUMED_USD, RESUMED_SHARE * before):
                    break
                found.append(again)
                since, blocked = again, min((h for h in seen["hits"] if h > again), default=None)
            for at in found:
                known = restarts.setdefault(email, [])
                if not any(abs(at - other) < 3600 for other in known):
                    known.append(at)
        # A drop between two readings is dated by the later one, unless the transcripts
        # (or another window's readings) already place a restart between them.
        for email, last, at in sorted(dropped, key=lambda drop: drop[2]):
            known = restarts.setdefault(email, [])
            if not any(last <= other <= at for other in known):
                known.append(at)

        rows = []
        for (email, label, end), seen in sorted(windows.items(), key=lambda item: (item[0][2], item[0][0], item[0][1])):
            scope = "" if label in ("5h", "7d") else label.casefold()
            start = end - SPAN.get(label, WEEK)
            if start < log_from:
                continue  # nothing tells whose usage it was before cswap's log begins
            # A restart of one window restarts them all, so every window of the account splits there.
            bounds = [(start, "reset")] + sorted((at, "restart") for at in restarts.get(email, [])
                                                 if start + 900 < at < end)
            for n, (t0, kind) in enumerate(bounds):
                t1 = bounds[n + 1][0] if n + 1 < len(bounds) else end
                hit = min((h for h in seen["hits"] if t0 < h <= t1), default=None)
                inside = [(at, pct) for at, pct in seen["readings"] if t0 <= at < t1 and (hit is None or at <= hit)]
                if hit is None and not inside:
                    continue
                # A restart seen in the readings is dated by the first low one, which may already be above zero.
                base = inside[0][1] if kind == "restart" and inside and inside[0][0] - t0 < 1 else 0.0
                until, pct = (hit, 100.0) if hit is not None else inside[-1]
                points, method = pct - base, "упор" if hit is not None else "уровень"
                total = spent(db, email, t0, until, scope)
                if hit is not None and total["usd"] <= 0:
                    continue  # a hit the log pinned on the wrong account, moments after a switch
                if hit is None and inside[0][1] - base >= 5 and pct - inside[0][1] >= MIN_BASELINE:
                    # The start of the segment was not watched: measure between the readings instead.
                    method, points = "разность", pct - inside[0][1]
                    total = spent(db, email, inside[0][0], until, scope)
                nobody = usd(None, t0, until, scope)
                first = db.execute("SELECT MIN(ts) FROM spend WHERE owner IS ? AND ts >= ? AND ts < ?",
                                   (email, t0, until)).fetchone()[0]
                flags = []
                if label == "5h" and kind == "reset" and first and first - t0 > OPENED_LATE:
                    flags.append("начато не здесь")
                rounding = 0 if hit is not None else 1
                low, high = total["usd"] - total["usd_edge"], total["usd"] + nobody
                row = {
                    "email": email, "window": label, "resets_at": end, "segment": n + 1, "of": len(bounds),
                    "from": t0, "from_kind": kind, "until": until, "pct": pct, "points": points, "method": method,
                    "plan": plan_for(plans.get(email, []), t0, until), "current": end > now and n + 1 == len(bounds),
                    "spent": total, "nobody_usd": nobody, "flags": flags, "full": None,
                }
                if points >= 1 and total["usd"] > 0:
                    row["full"] = {
                        "usd": total["usd"] / points * 100,
                        "low": low / (points + rounding) * 100,
                        "high": high / (points - rounding) * 100 if points > rounding else None,
                        "no_read_usd": (total["usd"] - total["usd_read"]) / points * 100,
                        "tokens": sum(total[part] for part in ("t_in", "t_out", "t_read", "t_write")) / points * 100,
                        "rough": hit is None and points < 10,
                    }
                rows.append(row)

        # Shares of what is uncertain, over the time the accounts are known for.
        totals = db.execute("SELECT SUM(usd), SUM(usd_guessed), SUM(usd * alone), SUM(usd * edge), "
                            "SUM(CASE WHEN owner IS NULL THEN usd ELSE 0 END) FROM spend WHERE ts >= ?", (log_from,)).fetchone()
        indexed = db.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM replies").fetchone()
        days = {}
        for series in readings.values():
            for at, _, _ in series:
                days.setdefault(time.strftime("%Y-%m-%d", time.localtime(at)), set()).add(time.localtime(at).tm_hour)
        data = {
            "now": now, "indexed": {"from": indexed[0], "to": indexed[1], "replies": indexed[2]},
            "log_from": moments[0] if moments else None,
            "usd": totals[0] or 0.0, "usd_guessed": totals[1] or 0.0, "usd_alone": totals[2] or 0.0,
            "usd_edge": totals[3] or 0.0, "usd_nobody": totals[4] or 0.0,
            "fits": {model: {"r2": fit[4], "n": fit[5]} for model, fit in fits.items() if rates(model)},
            "unpriced": unpriced, "checks": checks,
            "restarts": sorted(({"email": email, "at": at} for email, times in restarts.items() for at in times
                                if at >= log_from), key=lambda restart: restart["at"]),
            "plans": plans, "coverage": {day: len(hours) for day, hours in sorted(days.items())},
        }
    finally:
        db.close()
    return {"rows": rows, "data": data}
