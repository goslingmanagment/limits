"""Tokens of Claude and Codex per day for all time, from transcripts on this Mac and their archives.

Claude replies are parsed with limits' own parser (worth.parse) and deduplicated by
message id + request id across every file and archive, so resumed and forked sessions
count once. Codex usage is the growth of each session's cumulative token_count, with
repeated events skipped, deduplicated by (timestamp, totals) across homes and archives.

The result is kept in history/daily.csv and history/sessions.csv under the data directory.
A stored day is replaced only by a fuller one, so the totals survive transcripts being
moved away: run it before archiving old sessions.
"""

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from collections import defaultdict
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path
from zoneinfo import ZoneInfo

import worth
from metadata import parse_date

HOME = Path.home()

MSK = ZoneInfo("Europe/Moscow")
CLAUDE_ROOT = HOME / ".claude/projects"
COWORK_ROOT = HOME / "Library/Application Support/Claude/local-agent-mode-sessions"
ARCHIVE = HOME / "session-archive"
ORCA = HOME / "Library/Application Support/orca/codex-accounts"
CODEX_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


def h64(text):
    return int.from_bytes(hashlib.blake2b(text.encode(), digest_size=8).digest(), "big", signed=True)


# Claude -----------------------------------------------------------------------------

def claude_rows(lines, sub, cowork=False):
    """{key: [ts, model, desktop, sub, input, output, cache_read, write_5m, write_1h, fast]}"""
    rows = {}
    for line in lines:
        if not line.endswith(b"\n"):
            break
        if b'"assistant"' not in line or b'"usage"' not in line:
            continue
        if cowork:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not entry.get("timestamp") and entry.get("_audit_timestamp"):
                entry["timestamp"] = entry["_audit_timestamp"]
            line = json.dumps(entry).encode()
        found = worth.parse(line, 0, sub)
        if not found or found[0] != "reply":
            continue
        r = found[1]
        row = [r[1], r[2], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11]]
        old = rows.get(r[0])
        if old is None:
            rows[r[0]] = row
        else:
            old[0] = min(old[0], row[0])
            old[5] = max(old[5], row[5])
            old[9] = old[9] or row[9]
    return rows


def claude_where(rel):
    parts = rel.split("/")
    project = parts[0]
    session = parts[1] if len(parts) > 2 else Path(parts[-1]).stem
    return project, session


def scan_claude_file(path):
    rel = os.path.relpath(path, CLAUDE_ROOT)
    try:
        with open(path, "rb") as stream:
            rows = claude_rows(stream, "/subagents/" in path)
    except OSError:
        rows = {}
    return ("code", *claude_where(rel), rows)


def scan_cowork_file(path):
    rel = os.path.relpath(path, COWORK_ROOT)
    session = rel.split("/")[-2]
    try:
        with open(path, "rb") as stream:
            rows = claude_rows(stream, False, cowork=True)
    except OSError:
        rows = {}
    return ("cowork", "cowork", session, rows)


def scan_claude_archive(path):
    out = []
    zstd = subprocess.Popen(["zstd", "-dc", str(path)], stdout=subprocess.PIPE)
    with tarfile.open(fileobj=zstd.stdout, mode="r|") as tar:
        for member in tar:
            if not member.isfile() or not member.name.endswith(".jsonl") or "/._" in "/" + member.name:
                continue
            rel = member.name.lstrip("./")
            rows = claude_rows(tar.extractfile(member), "/subagents/" in rel)
            if rows:
                out.append(("code", *claude_where(rel), rows))
    zstd.wait()
    return out


# Codex ------------------------------------------------------------------------------

def codex_rows(lines, account):
    """{key: (ts, model, account, input, cached, output, reasoning, session, cwd)}"""
    model = session = cwd = None
    previous = None
    rows = {}
    for line in lines:
        if not line.endswith(b"\n"):
            break
        if b'"token_count"' not in line:
            if b'"turn_context"' in line or b'"session_meta"' in line:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
                if entry.get("type") == "turn_context" and isinstance(payload.get("model"), str):
                    model = payload["model"]
                elif entry.get("type") == "session_meta":
                    session = session or payload.get("id")
                    cwd = cwd or payload.get("cwd")
                    if not model and isinstance(payload.get("model"), str):
                        model = payload["model"]
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        info = payload.get("info")
        if payload.get("type") != "token_count" or not isinstance(info, dict):
            continue
        total, last = info.get("total_token_usage"), info.get("last_token_usage")
        total = tuple(int(total.get(f) or 0) for f in CODEX_FIELDS) if isinstance(total, dict) else None
        last = tuple(int(last.get(f) or 0) for f in CODEX_FIELDS) if isinstance(last, dict) else None
        if total is not None and previous is not None:
            delta = tuple(a - b for a, b in zip(total, previous))
            if not any(delta):
                continue  # the same totals emitted again
            if any(x < 0 for x in delta):
                delta = last or total  # the counter started over
        else:
            delta = last or total
        if total is not None:
            previous = total
        moment = parse_date(entry.get("timestamp"))
        if delta is None or not any(delta) or not moment:
            continue
        key = h64(f"{entry.get('timestamp')}|{total}|{last}")
        rows[key] = (moment.timestamp(), model or "unknown", account, *delta, session, cwd)
    return rows


def scan_codex_file(item):
    account, path = item
    try:
        if path.endswith(".zst"):
            zstd = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE)
            rows = codex_rows(zstd.stdout, account)
            zstd.wait()
        else:
            with open(path, "rb") as stream:
                rows = codex_rows(stream, account)
    except OSError:
        rows = {}
    return rows


def scan_codex_archive(item):
    account, path = item
    rows = {}
    zstd = subprocess.Popen(["zstd", "-dc", str(path)], stdout=subprocess.PIPE)
    with tarfile.open(fileobj=zstd.stdout, mode="r|") as tar:
        for member in tar:
            if member.isfile() and "/._" not in "/" + member.name and member.name.endswith((".jsonl", ".jsonl.zst")):
                data = tar.extractfile(member)
                if member.name.endswith(".zst"):
                    raw = subprocess.run(["zstd", "-dc"], input=data.read(), stdout=subprocess.PIPE).stdout
                    data = raw.splitlines(keepends=True)
                rows.update(codex_rows(data, account))
    zstd.wait()
    return rows


def walk(root, suffixes):
    for directory, _, names in os.walk(root):
        for name in names:
            if name.endswith(suffixes) and not name.startswith("._"):
                yield os.path.join(directory, name)


def codex_sources():
    homes = [("codex", HOME / ".codex")]
    if ORCA.exists():
        homes += [(f"orca-{d.name[:8]}", d / "home") for d in sorted(ORCA.iterdir()) if (d / "home").exists()]
    files = []
    for account, home in homes:
        for part in ("sessions", "archived_sessions"):
            if (home / part).exists():
                files += [(account, p) for p in walk(home / part, (".jsonl", ".jsonl.zst"))]
    archives = [(f"orca-{p.name[:8]}", p) for p in sorted((ARCHIVE / "orca-codex").glob("*.tar.zst"))]
    return files, archives


# Output -----------------------------------------------------------------------------

def day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(MSK).strftime("%Y-%m-%d")


def usd(model, inp, out, cache_read, w5, w1h):
    rates = worth.rates(model)
    if not rates:
        return None
    return (inp * rates[0] + out * rates[1] + cache_read * rates[2] + w5 * rates[3] + w1h * rates[4]) / 1e6


def build(out_dir=None):
    out_dir = Path(out_dir) if out_dir else worth.data_dir() / "history"
    out_dir.mkdir(parents=True, exist_ok=True)
    zstd = shutil.which("zstd") is not None
    claude = {}  # key -> row + [source, project, session]
    codex = {}

    claude_files = sorted(walk(CLAUDE_ROOT, (".jsonl",)))
    cowork_files = sorted(walk(COWORK_ROOT, (".jsonl",))) if COWORK_ROOT.exists() else []
    codex_files, codex_archives = codex_sources()
    claude_archives = sorted((ARCHIVE / "claude-code").glob("*.tar.zst"))
    if not zstd:
        skipped = len(claude_archives) + len(codex_archives) + sum(1 for _, p in codex_files if p.endswith(".zst"))
        print(f"zstd не найден: пропущено сжатых файлов и архивов: {skipped}", file=sys.stderr)
        claude_archives, codex_archives = [], []
        codex_files = [(a, p) for a, p in codex_files if not p.endswith(".zst")]
    print(f"Claude: файлов {len(claude_files)}, архивов {len(claude_archives)}; Cowork: {len(cowork_files)}; "
          f"Codex: файлов {len(codex_files)}, архивов {len(codex_archives)}", flush=True)

    def take_claude(result):
        source, project, session, rows = result
        for key, row in rows.items():
            old = claude.get(key)
            if old is None:
                claude[key] = row + [source, project, session]
            else:
                old[0] = min(old[0], row[0])
                old[5] = max(old[5], row[5])

    with Pool(max(2, min(12, (os.cpu_count() or 4) - 2))) as pool:
        archive_jobs = [pool.apply_async(scan_claude_archive, (a,)) for a in claude_archives]
        codex_archive_jobs = [pool.apply_async(scan_codex_archive, (a,)) for a in codex_archives]
        for result in pool.imap_unordered(scan_claude_file, claude_files, chunksize=16):
            take_claude(result)
        for result in pool.imap_unordered(scan_cowork_file, cowork_files, chunksize=16):
            take_claude(result)
        for job in archive_jobs:
            for result in job.get():
                take_claude(result)
        for rows in pool.imap_unordered(scan_codex_file, codex_files, chunksize=8):
            for key, row in rows.items():
                codex.setdefault(key, row)
        for job in codex_archive_jobs:
            for key, row in job.get().items():
                codex.setdefault(key, row)
    print(f"Ответов Claude: {len(claude)}; замеров Codex: {len(codex)}", flush=True)

    daily = defaultdict(lambda: [0] * 7)  # requests, input, cache_read, cache_write, output, total, usd
    sessions = defaultdict(lambda: {"first": None, "last": None, "n": 0, "total": 0, "output": 0, "usd": 0.0,
                                    "models": set()})
    unpriced = defaultdict(int)
    for ts, model, desktop, sub, inp, out, cr, w5, w1h, fast, source, project, session in claude.values():
        cost = usd(model, inp, out, cr, w5, w1h)
        if cost is None:
            unpriced[model] += 1
        total = inp + out + cr + w5 + w1h
        tool = "claude-cowork" if source == "cowork" else ("claude-desktop" if desktop else "claude-code")
        d = daily[(day(ts), "claude", tool, "", model, "subagent" if sub else "main")]
        for i, v in enumerate((1, inp, cr, w5 + w1h, out, total, cost or 0.0)):
            d[i] += v
        s = sessions[("claude", tool, project, session)]
        s["first"] = ts if s["first"] is None else min(s["first"], ts)
        s["last"] = ts if s["last"] is None else max(s["last"], ts)
        s["n"] += 1
        s["total"] += total
        s["output"] += out
        s["usd"] += cost or 0.0
        s["models"].add(model)
    for ts, model, account, inp, cached, out, reasoning, session, cwd in codex.values():
        total = inp + out
        d = daily[(day(ts), "codex", "codex", account, model, "main")]
        for i, v in enumerate((1, inp - cached, cached, 0, out, total, 0.0)):
            d[i] += v
        s = sessions[("codex", account, cwd or "", session or "")]
        s["first"] = ts if s["first"] is None else min(s["first"], ts)
        s["last"] = ts if s["last"] is None else max(s["last"], ts)
        s["n"] += 1
        s["total"] += total
        s["output"] += out
        s["models"].add(model)

    stored = read_csv(out_dir / "daily.csv")
    # A stored day already holds what the snapshot could add, split by tool; refilling it from
    # the snapshot after its transcripts moved away would trade that split for one coarse row.
    restored = restore_from_ccusage(daily, skip_days={row[0] for row in stored})
    print(f"Дополнено из сводок ccusage: {restored / 1e9:.2f} млрд токенов", flush=True)

    daily_header = ["date", "vendor", "tool", "account", "model", "scope", "requests", "input", "cache_read",
                    "cache_write", "output", "total", "usd_api"]
    new_daily = [[*key, *v[:6], round(v[6], 4)] for key, v in daily.items()]
    fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).astimezone(MSK).strftime("%Y-%m-%d %H:%M")
    session_header = ["vendor", "tool", "project", "session", "first_msk", "last_msk", "requests", "total",
                      "output", "usd_api", "models"]
    new_sessions = [[*key, fmt(s["first"]), fmt(s["last"]), s["n"], s["total"], s["output"], round(s["usd"], 4),
                     " ".join(sorted(s["models"]))] for key, s in sessions.items()]

    # A stored day is replaced only by a fuller one, so moving transcripts away never lowers it.
    by_day = defaultdict(lambda: [[], []])
    for row in stored:
        by_day[row[0]][0].append(row)
    for row in new_daily:
        by_day[row[0]][1].append(row)
    kept_days = 0
    merged = []
    for date, (old, new) in by_day.items():
        total = lambda rows: sum(int(r[11]) for r in rows)
        if old and total(old) >= total(new):
            merged += old
            kept_days += 1
        else:
            merged += new
    write_csv(out_dir / "daily.csv", daily_header, sorted(merged, key=lambda r: tuple(map(str, r[:6]))))

    sessions_by_key = {tuple(r[:4]): r for r in read_csv(out_dir / "sessions.csv")}
    for row in new_sessions:
        key = tuple(map(str, row[:4]))
        if key not in sessions_by_key or int(sessions_by_key[key][6]) <= row[6]:
            sessions_by_key[key] = row
    write_csv(out_dir / "sessions.csv", session_header, sorted(sessions_by_key.values(), key=lambda r: str(r[4])))
    print(f"Дней оставлено из сохранённой копии: {kept_days}; модели без цены: {dict(unpriced) or 'нет'}", flush=True)
    print(f"Готово: {out_dir / 'daily.csv'}, {out_dir / 'sessions.csv'}", flush=True)
    return 0


def restore_from_ccusage(daily, skip_days=()):
    """Adds what ccusage saw on a day and the transcripts no longer hold (files deleted since), per model.

    Days in skip_days are left alone: the stored copy already has them."""
    added = 0
    for path in sorted((ARCHIVE / "claude-code").glob("ccusage-daily-*.json")):
        taken = path.stem.rsplit("-", 3)[-3:]
        snapshot_day = "-".join(taken)
        mine = defaultdict(lambda: [0, 0, 0, 0])  # input, output, cache_read, cache_write
        for (date, vendor, tool, account, model, scope), v in daily.items():
            if vendor == "claude" and tool != "claude-cowork":
                m = mine[(date, model)]
                m[0] += v[1]
                m[1] += v[4]
                m[2] += v[2]
                m[3] += v[3]
        for entry in json.load(open(path)).get("daily", []):
            date = entry.get("period")
            if not date or date >= snapshot_day or date in skip_days:
                continue  # the snapshot's last day was still going, or the day is stored already
            for b in entry.get("modelBreakdowns", []):
                model = b.get("modelName", "")
                theirs = (b.get("inputTokens", 0), b.get("outputTokens", 0), b.get("cacheReadTokens", 0),
                          b.get("cacheCreationTokens", 0))
                have = mine[(date, model)]
                if sum(theirs) <= sum(have) * 1.001:
                    continue
                gap = [max(0, t - h) for t, h in zip(theirs, have)]
                cost = usd(model, gap[0], gap[1], gap[2], gap[3], 0) or 0.0
                d = daily[(date, "claude", "claude-code", "", model, f"restored-{snapshot_day}")]
                for i, value in ((1, gap[0]), (2, gap[2]), (3, gap[3]), (4, gap[1]), (5, sum(gap)), (6, cost)):
                    d[i] += value
                added += sum(gap)
    return added


def read_csv(path):
    if not path.exists():
        return []
    with open(path, newline="") as stream:
        rows = list(csv.reader(stream))
    return rows[1:]


def write_csv(path, header, rows):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="") as stream:
        w = csv.writer(stream, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
    os.replace(tmp, path)
    worth.private(path)


def main():
    return build(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    sys.exit(main())
