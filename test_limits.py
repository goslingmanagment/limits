import base64
import contextlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import metadata
import stats
import watch
import worth


def write_auth(home, email, account_id="personal", until="2026-10-10T15:19:46Z", checked="2026-09-21T19:32:00Z"):
    home.mkdir(parents=True, exist_ok=True)
    payload = {"email": email, "exp": 9999999999, "https://api.openai.com/auth": {
        "chatgpt_account_id": account_id, "chatgpt_plan_type": "pro",
        "chatgpt_subscription_active_until": until,
        "chatgpt_subscription_last_checked": checked,
    }}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": "fake." + encoded + ".fake", "account_id": account_id}}))


def usage(email, pct=25):
    return {"account": email, "usage": {"accountEmail": email,
            "updatedAt": "2026-09-28T03:00:00Z",
            "secondary": {"usedPercent": pct, "resetsAt": "2026-10-03T17:00:00Z"}}}


class Accounts(unittest.TestCase):
    def test_orca_union_is_deduplicated_by_workspace(self):
        with tempfile.TemporaryDirectory() as tmp, patch("pathlib.Path.home", return_value=Path(tmp)), patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            write_auth(root / ".codex", "first@example.com")
            orca = root / "Library/Application Support/orca/codex-accounts"
            write_auth(orca / "a/home", "first@example.com")
            write_auth(orca / "b/home", "second@example.com")
            write_auth(orca / "c/home", "first@example.com", account_id="team")
            items, homes = metadata.discover_codex()
            self.assertEqual(len(items), 3)
            self.assertEqual(set(homes), {str(orca / "b/home"), str(orca / "c/home")})
            self.assertEqual(items[0]["sources"], ["System", "Orca"])

    def test_auth_expiry_is_not_subscription_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_auth(Path(tmp), "a@example.com", until=None)
            self.assertIsNone(metadata.codex_identity(tmp)["subscription"])

    def test_workspace_mismatch_does_not_get_subscription(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_auth(Path(tmp), "a@example.com")
            result = metadata.codex_identity(tmp, {"email": "a@example.com", "workspaceAccountID": "other"})
            self.assertIsNone(result["subscription"])

    def test_newest_checked_date_wins(self):
        with tempfile.TemporaryDirectory() as tmp, patch("pathlib.Path.home", return_value=Path(tmp)), patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            write_auth(root / ".codex", "a@example.com", checked="2026-09-28T00:00:00Z")
            write_auth(root / "Library/Application Support/orca/codex-accounts/a/home", "a@example.com", until="2026-10-01T00:00:00Z")
            items, homes = metadata.discover_codex()
            self.assertEqual(items[0]["subscription"]["until"], "2026-10-10T15:19:46Z")
            self.assertEqual(homes, [])

    def test_partial_failure_keeps_success_and_failed_account(self):
        payload = [usage("a@example.com"), {"account": "b@example.com", "error": {"message": "401 unauthorized"}}]
        with patch.object(watch, "discover_codex", return_value=([], [])), patch.object(watch, "run", return_value=json.dumps(payload)):
            accounts = watch.fetch_codex()
        self.assertEqual(len(accounts), 2)
        self.assertEqual(accounts[0]["windows"][0]["pct"], 25)
        self.assertEqual(accounts[1]["error"], "нужен повторный вход")

    def test_nonzero_process_can_still_return_account_json(self):
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, '[{"account":"b@example.com"}]', "error")):
            self.assertEqual(json.loads(watch.run(["test"]))[0]["account"], "b@example.com")

    def test_unreported_local_account_stays_visible(self):
        inventory = [{"email": "b@example.com", "account_id": "b", "subscription": None}]
        with patch.object(watch, "discover_codex", return_value=(inventory, [])), patch.object(watch, "run", return_value=json.dumps([usage("a@example.com")])):
            accounts = watch.fetch_codex()
        self.assertEqual([a["email"] for a in accounts], ["a@example.com", "b@example.com"])
        self.assertTrue(accounts[1]["error"])

    def test_same_email_workspaces_do_not_borrow_billing_dates(self):
        inventory = [{"email": "a@example.com", "account_id": k, "subscription": {"until": "2026-10-10"}} for k in ("a", "b")]
        with patch.object(watch, "discover_codex", return_value=(inventory, [])), patch.object(watch, "run", return_value=json.dumps([usage("a@example.com")])):
            accounts = watch.fetch_codex()
        self.assertIsNone(accounts[0].get("subscription"))

    def test_last_good_usage_preserves_original_timestamp(self):
        old = [{"email": "a@example.com", "windows": [{"pct": 25}], "fetched_at": "2026-09-20T00:00:00Z"}]
        new = [{"email": "a@example.com", "windows": [], "error": "offline"}]
        result = watch.merge_last_good(old, new)[0]
        self.assertEqual(result["fetched_at"], old[0]["fetched_at"])
        self.assertEqual(result["windows"], old[0]["windows"])
        self.assertEqual(result["error"], "offline")

    def test_orca_paths_passed_only_in_temporary_config(self):
        def probe(cmd, timeout, env):
            config = Path(env["CODEXBAR_CONFIG"])
            self.assertEqual(json.loads(config.read_text())["providers"][0]["codexProfileHomePaths"], ["/test/orca/home"])
            self.assertIn("--all-accounts", cmd)
            self.assertEqual(cmd[cmd.index("--source") + 1], "oauth")
            return json.dumps([usage("a@example.com")])
        with patch.object(watch, "discover_codex", return_value=([], ["/test/orca/home"])), patch.object(watch, "run", side_effect=probe):
            watch.fetch_codex()

    def test_last_good_usage_fills_in_when_cswap_cannot_fetch(self):
        payload = {"accounts": [{"email": "a@example.com", "number": 1, "usageStatus": "unavailable", "usage": None,
                                 "lastGoodUsage": {"sevenDay": {"pct": 40, "resetsAt": "2026-10-01T14:00:00Z"}},
                                 "lastGoodFetchedAt": "2026-09-28T03:00:00Z"}]}
        with patch.object(watch, "claude_plans", return_value={}), patch.object(watch, "run", return_value=json.dumps(payload)):
            account = watch.fetch_claude()[0]
        self.assertEqual([(w["label"], w["pct"]) for w in account["windows"]], [("7d", 40.0)])
        self.assertEqual(account["fetched_at"], "2026-09-28T03:00:00Z")
        notice = watch.notice_text(account, None, datetime(2026, 9, 28, 3, 20, tzinfo=timezone.utc))
        self.assertEqual(notice, "не удалось обновить · последние данные 20m назад")

    def test_cswap_error_is_reported_in_its_own_words(self):
        envelope = {"schemaVersion": 1, "error": {"type": "ClaudeSwitchError", "message": "No accounts configured"}}
        for out, message in ((json.dumps(envelope), "claude-swap: No accounts configured"),
                             ("", "claude-swap не отвечает (cswap list --json)")):
            with patch.object(watch, "run", return_value=out), self.assertRaises(watch.FetchError) as caught:
                watch.fetch_claude()
            self.assertEqual(str(caught.exception), message)

    def test_passed_reset_shows_the_window_as_it_is_now(self):
        now = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
        passed = (now - timedelta(hours=1)).isoformat()
        week = {"label": "7d", "pct": 99, "resets_at": passed, "runs_out_at": passed, "ahead": True, "clock": "x"}
        five = {"label": "5h", "pct": 70, "resets_at": passed}
        later = {"label": "Fable", "pct": 35, "resets_at": (now + timedelta(days=1)).isoformat()}
        claude = watch.roll_over([dict(provider="claude", windows=[week, five, later])], now)[0]["windows"]
        self.assertEqual((claude[0]["pct"], claude[0]["runs_out_at"], claude[0]["ahead"]), (0.0, None, False))
        self.assertEqual(watch.parse_date(claude[0]["resets_at"]), now - timedelta(hours=1) + timedelta(days=7))
        self.assertEqual((claude[1]["pct"], claude[1]["resets_at"]), (0.0, None))
        self.assertIs(claude[2], later)
        codex = watch.roll_over([dict(provider="codex", windows=[week])], now)[0]["windows"][0]
        self.assertEqual((codex["pct"], codex["resets_at"]), (0.0, None))
        self.assertIsNone(watch.roll_over(None, now))
        self.assertEqual(week["pct"], 99)

    def test_bad_window_does_not_hide_another_account(self):
        payload = [usage("a@example.com", "NaN"), usage("b@example.com", 10)]
        with patch.object(watch, "discover_codex", return_value=([], [])), patch.object(watch, "run", return_value=json.dumps(payload)):
            accounts = watch.fetch_codex()
        self.assertEqual(accounts[0]["windows"], [])
        self.assertTrue(accounts[0]["error"])
        self.assertEqual(accounts[1]["windows"][0]["pct"], 10)


class Subscriptions(unittest.TestCase):
    def test_confirmed_dates_roundtrip_without_monthly_rollover(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LIMITS_SUBSCRIPTIONS": tmp + "/dates.json"}):
            metadata.save_subscription("claude", "A@example.com", "2026-10-07", renews=True, source="billing")
            entry = metadata.load_subscriptions()["claude:a@example.com"]
            text, warn = watch.subscription_text(entry, now=datetime(2026, 11, 1, tzinfo=timezone.utc))
            self.assertEqual(text, "продление 07.10 — проверь")
            self.assertTrue(warn)
            self.assertEqual(os.stat(tmp + "/dates.json").st_mode & 0o777, 0o600)

    def test_corrupt_dates_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"LIMITS_SUBSCRIPTIONS": tmp + "/dates.json"}):
            path = Path(tmp) / "dates.json"
            path.write_text("broken")
            with self.assertRaises(ValueError):
                metadata.save_subscription("claude", "a@example.com", "2026-10-07")
            self.assertEqual(path.read_text(), "broken")

    def test_date_only_is_not_shifted_by_timezone(self):
        now = datetime(2026, 9, 28, tzinfo=timezone.utc)
        try:
            with patch.dict(os.environ, {"TZ": "America/Los_Angeles"}):
                time.tzset()
                text = watch.subscription_text({"until": "2026-10-07", "kind": "renews"}, now)
        finally:
            time.tzset()
        self.assertEqual(text, ("продление 07.10", False))

    def test_other_year_and_end_of_period_are_spelled_out(self):
        text, warn = watch.subscription_text({"until": "2027-01-15T10:00:00Z"}, datetime(2026, 9, 28, tzinfo=timezone.utc))
        self.assertEqual((text, warn), ("оплачено до 15.01.2027", False))

    def test_dates_in_the_next_five_days_are_flagged(self):
        now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        def text(until):
            return watch.subscription_text({"until": until, "kind": "renews"}, now)
        self.assertEqual(text("2026-09-28"), ("продление 28.09 (сегодня)", True))
        self.assertEqual(text("2026-09-29"), ("продление 29.09 (завтра)", True))
        self.assertEqual(text("2026-10-03"), ("продление 03.10 (5 дн.)", True))
        self.assertEqual(text("2026-10-04"), ("продление 04.10", False))

    def test_unknown_does_not_infer_from_creation_or_limit_reset(self):
        self.assertEqual(watch.subscription_text({"createdAt": "2026-09-10", "resetsAt": "2026-10-10"}),
                         ("нет даты подписки", False))


class Display(unittest.TestCase):
    def test_ansi_clipping_does_not_break_escape_sequences(self):
        line = watch.BOLD + "x" * 200 + watch.RESET
        clipped = watch.clip_line(line, 79)
        self.assertEqual(watch.ANSI.sub("", clipped), "x" * 79)
        self.assertTrue(clipped.endswith(watch.RESET))

    def test_five_accounts_fit_120_by_24(self):
        window = {"pct": 20, "label": "7d", "resets_at": "2026-10-03T17:00:00Z", "clock": "03.10 20:00", "ahead": True}
        claude = [dict(provider="claude", email=f"claude{i}@example.com", active=False, windows=[window]*3) for i in range(3)]
        codex = [dict(provider="codex", email=f"codex{i}@example.com", active=False, windows=[window]) for i in range(2)]
        with patch.object(watch.shutil, "get_terminal_size", return_value=os.terminal_size((120, 24))):
            text = watch.ANSI.sub("", watch.render(claude, codex, 0, 0, []))
        self.assertLessEqual(len(text.splitlines()), 23)
        self.assertTrue(all(len(line) <= 119 for line in text.splitlines()))
        self.assertIn("codex1", text)

    def test_countdown(self):
        now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(watch.countdown((now + timedelta(days=1, hours=10, minutes=5)).isoformat(), now), "1d 10h")
        self.assertEqual(watch.countdown((now + timedelta(hours=3, minutes=12)).isoformat(), now), "3h 12m")
        self.assertEqual(watch.countdown((now - timedelta(minutes=1)).isoformat(), now), "сейчас")
        self.assertEqual(watch.countdown(None, now), "")

    def test_alert_names_when_the_limit_runs_out(self):
        now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        soon = (now + timedelta(hours=1, minutes=18)).isoformat()
        self.assertIn("кончится через ~1h 18m", watch.pace_alert({"pct": 99, "runs_out_at": soon}, now))
        self.assertIn("исчерпан", watch.pace_alert({"pct": 100, "runs_out_at": soon}, now))
        self.assertIn("быстрее нормы", watch.pace_alert({"pct": 40, "ahead": True, "runs_out_at": None}, now))
        self.assertEqual(watch.pace_alert({"pct": 40, "runs_out_at": None}, now), "")

    def test_claude_projection_only_when_it_runs_out_before_reset(self):
        week = {"pct": 99, "resetsAt": "2026-09-29T14:00:00Z", "willLastToReset": False,
                "projectedExhaustionAt": "2026-09-28T04:40:32Z"}
        scoped = {"pct": 35, "name": "Fable", "willLastToReset": True,
                  "projectedExhaustionAt": "2026-10-08T10:56:23Z"}
        payload = {"accounts": [{"email": "a@example.com", "usage": {"sevenDay": week, "scoped": [scoped]}}]}
        with patch.object(watch, "claude_plans", return_value={}), patch.object(watch, "run", return_value=json.dumps(payload)):
            windows = watch.fetch_claude()[0]["windows"]
        self.assertEqual([w["runs_out_at"] for w in windows], ["2026-09-28T04:40:32Z", None])

    def test_codex_projection_is_anchored_to_the_fetch(self):
        entry = usage("a@example.com", 40)
        entry["pace"] = {"secondary": {"stage": "farAhead", "willLastToReset": False, "etaSeconds": 3600}}
        with patch.object(watch, "discover_codex", return_value=([], [])), patch.object(watch, "run", return_value=json.dumps([entry])):
            window = watch.fetch_codex()[0]["windows"][0]
        left = (watch.parse_date(window["runs_out_at"]) - datetime.now(timezone.utc)).total_seconds()
        self.assertTrue(3500 < left <= 3600)

    def test_alerts_stay_whole_and_only_claude_rows_have_slots(self):
        now = datetime.now(timezone.utc)
        window = {"pct": 82, "label": "7d", "resets_at": (now + timedelta(days=2, hours=22)).isoformat(),
                  "runs_out_at": (now + timedelta(hours=21, minutes=30)).isoformat()}
        claude = [dict(provider="claude", email="alice@example.com", slot=3, plan="Max 20x", active=True, windows=[window])]
        codex = [dict(provider="codex", email="bob@example.com", plan="pro", active=False, windows=[window])]
        for columns in (100, 80):
            with patch.object(watch.shutil, "get_terminal_size", return_value=os.terminal_size((columns, 40))):
                lines = watch.ANSI.sub("", watch.render(claude, codex, 0, 0, [])).splitlines()
            self.assertTrue(all(len(line) < columns for line in lines))
            self.assertEqual(sum("кончится через ~21h" in line for line in lines), 1)
            self.assertTrue(next(l for l in lines if "alice" in l).lstrip().startswith("3  alice"))
            self.assertTrue(next(l for l in lines if "bob" in l).lstrip().startswith("bob"))

    def test_forecast_only_for_the_active_claude_account(self):
        now = datetime.now(timezone.utc)
        window = {"pct": 82, "label": "7d", "resets_at": (now + timedelta(days=2)).isoformat(),
                  "runs_out_at": (now + timedelta(hours=5)).isoformat(), "ahead": True}
        spent = {"pct": 100, "label": "5h", "resets_at": (now + timedelta(hours=2)).isoformat()}
        claude = [dict(provider="claude", email=f"{name}@example.com", slot=i, active=i == 1, windows=[window, spent])
                  for i, name in ((1, "alice"), (2, "carol"))]
        codex = [dict(provider="codex", email="bob@example.com", active=False, windows=[window])]
        with patch.object(watch.shutil, "get_terminal_size", return_value=os.terminal_size((120, 40))):
            lines = watch.ANSI.sub("", watch.render(claude, codex, 0, 0, [])).splitlines()
        forecasts = [line for line in lines if "кончится через" in line or "быстрее нормы" in line]
        self.assertEqual(len(forecasts), 1)
        self.assertIn("alice", forecasts[0])
        self.assertEqual(sum("исчерпан" in line for line in lines), 2)

    def test_tab_title_sums_up_the_active_account(self):
        now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        calm = {"pct": 36, "label": "7d"}
        busy = {"pct": 82, "label": "7d", "runs_out_at": (now + timedelta(hours=5)).isoformat()}
        def account(active, *windows):
            return dict(provider="claude", email="alice@example.com", active=active, windows=list(windows))
        self.assertEqual(watch.tab_title([account(True, {"pct": 9, "label": "Fable"}, calm)], now), "limits · alice 36%")
        self.assertEqual(watch.tab_title([account(True, calm, busy)], now), "limits · alice 82% ▲")
        self.assertEqual(watch.tab_title([account(False, busy)], now), "limits")
        self.assertEqual(watch.tab_title(None, now), "limits")

    def test_account_column_stacks_plan_and_date_under_the_name(self):
        acc = dict(provider="codex", email="bob@example.com", plan="pro", active=False, windows=[],
                   subscription={"until": "2026-10-02T20:04:00Z"})
        with patch.object(watch.shutil, "get_terminal_size", return_value=os.terminal_size((100, 40))):
            text = watch.render([], [acc], 0, 0, [], now=datetime(2026, 9, 28, tzinfo=timezone.utc))
        lines = [line.strip() for line in watch.ANSI.sub("", text).splitlines()]
        row = next(i for i, line in enumerate(lines) if line.startswith("bob"))
        self.assertEqual(lines[row + 1], "Pro")
        self.assertEqual(lines[row + 2], "оплачено до 02.10 (4 дн.)")

    def test_email_domain_is_hidden_only_when_shared(self):
        same = [dict(email="a@gmail.com"), dict(email="b@Gmail.com")]
        self.assertEqual(watch.shared_domain(same), "gmail.com")
        self.assertEqual(watch.shared_domain(same + [dict(email="c@corp.com")]), "")
        self.assertEqual(watch.shared_domain([dict(email="codex")]), "")

    def test_normal_cache_age_is_not_reported(self):
        now = datetime.now(timezone.utc)
        def account(minutes):
            return dict(provider="claude", email="a@example.com", active=False, windows=[{"pct": 1, "label": "7d"}],
                        fetched_at=(now - timedelta(minutes=minutes)).isoformat())
        self.assertEqual(watch.notice_text(account(6), None, now), "")
        self.assertEqual(watch.notice_text(account(20), None, now), "данные 20m назад")



def reply(at, model="claude-opus-5-5", session="cli", output=100, msg="m1", desktop=False, stop="end_turn",
          speed="standard", block=None, index=0):
    """One transcript line of an assistant reply, as Claude Code writes it; stop=None is a copy mid-stream."""
    usage = {"input_tokens": 1000, "output_tokens": output, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0,
             "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}
    if stop:
        usage["speed"] = speed
    return json.dumps({
        "type": "assistant", "timestamp": at.isoformat().replace("+00:00", "Z"), "requestId": "r-" + msg,
        "sessionId": session, "entrypoint": "claude-desktop" if desktop else "cli", "uuid": f"u-{msg}-{index}",
        "apiBlockIndex": index,
        "message": {"id": msg, "model": model, "stop_reason": stop, "usage": usage,
                    "content": [block or {"type": "text", "text": "ok"}]},
    }) + "\n"


def hit(at, kind, resets, session="cli", text=None):
    """The line Claude Code writes when a request is refused for a limit."""
    line = {"type": "assistant", "timestamp": at.isoformat().replace("+00:00", "Z"), "sessionId": session,
            "entrypoint": "cli", "uuid": f"hit-{at.timestamp()}-{session}", "error": "rate_limit",
            "message": {"id": "x", "model": "<synthetic>", "usage": {"input_tokens": 0, "output_tokens": 0},
                        "content": [{"type": "text", "text": text or "You've hit your limit"}]}}
    if not text:
        line["quotaLimits"] = {"status": "rejected", "rateLimitType": kind, "resetsAt": int(resets.timestamp())}
    return json.dumps(line) + "\n"


def log_line(at, text):
    return datetime.fromtimestamp(at.timestamp()).strftime("%Y-%m-%d %H:%M:%S,000") + " - INFO - " + text + "\n"


MILLION = 1_000_000
OPUS_OUT = 20.0  # dollars per million output tokens of Opus 5.5


class Statistics(unittest.TestCase):
    NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)

    def home(self, stack):
        """A home directory of its own: cswap log, transcripts, Desktop records and limits' data live under it."""
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch("pathlib.Path.home", return_value=tmp))
        env = stack.enter_context(patch.dict(os.environ, {"XDG_CACHE_HOME": str(tmp / "cache"),
                                                          "XDG_DATA_HOME": str(tmp / "data")}))
        env.pop("CLAUDE_CONFIG_DIR", None)
        (tmp / ".claude-swap-backup").mkdir()
        (tmp / ".claude/projects/p").mkdir(parents=True)
        worth._seen.clear()
        worth._records.clear()
        return tmp

    def account(self, email, at, **windows):
        return dict(provider="claude", email=email, org="org-" + email[0], active=True, fetched_at=at.isoformat(),
                    windows=[{"label": label, "pct": float(pct), "resets_at": resets and resets.isoformat()}
                             for label, (pct, resets) in windows.items()])

    def test_index_counts_a_reply_once_and_keeps_limit_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, at = Path(tmp) / "projects", self.NOW - timedelta(hours=1)
            root.mkdir()
            transcript = root / "s.jsonl"
            thinking = {"type": "thinking", "thinking": "", "signature": "s" * 100}
            call = {"type": "tool_use", "input": {"path": "a" * 50}}
            done = (reply(at, output=8, stop=None, block=thinking)
                    + reply(at + timedelta(seconds=5), output=120, speed="fast", block=call, index=1) + "{broken\n"
                    + hit(at, "seven_day", self.NOW + timedelta(days=2))
                    + hit(at, None, None, "old", "You've hit your session limit · resets 3:30pm (Europe/Moscow)"))
            transcript.write_text(done + reply(at, msg="m2")[:40])
            db = worth.open_index(Path(tmp) / "index.sqlite3")
            worth.update_index(db, root, self.NOW.timestamp())
            rows = db.execute("SELECT ts, output, fast, final, seen, text_len, args_len, sig_len FROM replies").fetchall()
            self.assertEqual(rows, [(at.timestamp(), 120, 1, 1, 3, 0, len(json.dumps(call["input"])), 100)])
            self.assertEqual(db.execute("SELECT offset FROM files").fetchone()[0], len(done.encode()))
            hits = db.execute("SELECT kind, resets FROM hits ORDER BY kind").fetchall()
            self.assertEqual(hits, [("five_hour", (self.NOW + timedelta(minutes=30)).timestamp()),
                                    ("seven_day", (self.NOW + timedelta(days=2)).timestamp())])
            transcript.write_text(done + reply(at, msg="m2"))
            worth.update_index(db, root, self.NOW.timestamp())
            self.assertEqual(db.execute("SELECT count(*) FROM replies").fetchone()[0], 2)
            (root / "new").write_text(done + reply(at, msg="m2"))  # replaced by a file of the same size: read again
            os.replace(root / "new", transcript)
            worth.update_index(db, root, self.NOW.timestamp())
            self.assertEqual(db.execute("SELECT sig_len, args_len FROM replies ORDER BY sig_len DESC").fetchone(),
                             (100, len(json.dumps(call["input"]))))
            self.assertEqual(db.execute("SELECT count(*) FROM hits").fetchone()[0], 2)
            worth.update_index(db, root, (self.NOW + timedelta(days=91)).timestamp())
            self.assertEqual(db.execute("SELECT count(*) FROM replies").fetchone()[0], 0)
            self.assertEqual(os.stat(Path(tmp) / "index.sqlite3").st_mode & 0o777, 0o600)

    def test_prices_follow_the_model_and_unknown_models_have_none(self):
        self.assertEqual(worth.rates("claude-opus-5-5"), (4, 20, 0.2, 5, 8))
        self.assertEqual(worth.rates("claude-fable-5-1")[2], 0.25)
        self.assertEqual(worth.rates("claude-sonnet-5-5"), worth.rates("claude-sonnet-5"))
        self.assertIsNone(worth.rates("claude-opus-6"))

    def test_cswap_log_follows_slots_and_unknown_credentials(self):
        t = [self.NOW + timedelta(minutes=i) for i in range(11)]
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "claude-swap.log.1").write_text(
                log_line(t[0], "Added account 1: a@example.com (org: org-a)")
                + log_line(t[1], "Added account 2 from export: b@example.com"))
            Path(tmp, "claude-swap.log").write_text(
                log_line(t[2], "Switched from account 1 to 2")
                + log_line(t[3], "Swapped slots: 1 (a@example.com) <-> 2 (b@example.com)")
                + log_line(t[4], "Switched from account 1 to 2")
                + log_line(t[5], "Live credential resolves to a different account than Account-2's identity; backup left")
                + log_line(t[6], "Moved slot: 2 (a@example.com) -> 5")
                + log_line(t[7], "Resynced account 5's backup to the rotated live credential")
                + log_line(t[9], "Live credential does not belong to Account-5 (foreign): stashed as x "
                                 f"(credentials mtime {t[8].isoformat().replace('+00:00', 'Z')}). Something outside")
                + log_line(t[10], "Activated account 1 (no prior live account)"))
            history = worth.account_history(tmp)
        self.assertEqual(history, [(t[0].timestamp(), "a@example.com"), (t[2].timestamp(), "b@example.com"),
                                   (t[4].timestamp(), "a@example.com"), (t[5].timestamp(), None),
                                   (t[7].timestamp(), "a@example.com"), (t[8].timestamp(), None),
                                   (t[10].timestamp(), "b@example.com")])

    def test_observations_keep_readings_plans_and_desktop_sessions(self):
        now, week = self.NOW, self.NOW + timedelta(days=2)
        with contextlib.ExitStack() as stack:
            home = self.home(stack)
            (home / "data/limits").mkdir(parents=True)
            (home / "data/limits/history.jsonl").write_text(json.dumps({
                "measured_until": (now - timedelta(hours=3)).isoformat(), "email": "a@example.com", "window": "7d",
                "pct": 30.0, "resets_at": week.isoformat()}) + "\n")
            (home / ".claude.json").write_text(json.dumps({"oauthAccount": {
                "emailAddress": "a@example.com", "organizationType": "claude_max",
                "organizationRateLimitTier": "default_claude_max_5x"}}))
            record = home / "Library/Application Support/Claude/claude-code-sessions/user/org-a/local_1.json"
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps({"cliSessionId": "desk"}))
            def read(minutes, pct, resets=week):
                at = now + timedelta(minutes=minutes)
                return worth.observe([self.account("a@example.com", at, **{"7d": (pct, resets)})], at.timestamp())
            self.assertEqual(read(0, 40), 4)   # the earlier history, a reading, the plan, the Desktop session
            self.assertEqual(read(0, 40), 0)   # the same read of cswap again
            self.assertEqual(read(5, 40), 0)   # a newer read, nothing changed: held back
            worth._seen.clear()                # another process: it reads the file, not its own memory
            self.assertEqual(read(10, 40, week + timedelta(seconds=1)), 0)  # the reset drifts by a second
            self.assertEqual(read(15, 40), 0)
            self.assertEqual(read(20, 41), 2)  # the change, and before it the last reading of the old value
            (home / ".claude.json").write_text(json.dumps({"oauthAccount": {
                "emailAddress": "a@example.com", "organizationType": "claude_max",
                "organizationRateLimitTier": "default_claude_max_20x"}}))
            record.unlink()
            self.assertEqual(read(25, 4), 2)   # a restart of the percentage and the new plan
            readings, plans, sessions = worth.load_observations()
            self.assertEqual(os.stat(worth.observations_path()).st_mode & 0o777, 0o600)
        minutes = [(round((at - now.timestamp()) / 60), pct) for at, pct, _ in readings[("a@example.com", "7d")]]
        self.assertEqual(minutes, [(-180, 30.0), (0, 40.0), (15, 40.0), (20, 41.0), (25, 4.0)])
        self.assertEqual([plan for _, plan in plans["a@example.com"]], ["Max 5x", "Max 20x"])
        self.assertEqual(sessions, {"desk": "a@example.com"})

    def test_a_hit_measures_exactly_and_a_restart_starts_a_new_segment(self):
        now, hour = self.NOW, timedelta(hours=1)
        week = now + timedelta(days=2)          # a's weekly reset; its window began five days ago
        start = week - timedelta(days=7)
        with contextlib.ExitStack() as stack:
            home = self.home(stack)
            (home / ".claude-swap-backup/claude-swap.log").write_text(
                log_line(start - 10 * hour, "Added account 1: a@example.com (org: org-a)")
                + log_line(now - 30 * hour, "Added account 2: b@example.com (org: org-b)")
                + log_line(now - 29 * hour, "Switched from account 2 to 1"))
            record = home / "Library/Application Support/Claude/claude-code-sessions/user/org-a/local_1.json"
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps({"cliSessionId": "desk"}))
            first_hit = now - 20 * hour
            (home / ".claude/projects/p/t.jsonl").write_text(
                reply(start + hour, output=50 * MILLION, msg="a1")                       # $1000 before the hit
                + reply(start + 2 * hour, output=MILLION, msg="f1", speed="fast")        # fast mode: not the limit's
                + reply(now - 30 * hour, output=10 * MILLION, msg="b1")                  # ran as b
                + reply(now - 30 * hour, output=5 * MILLION, msg="d1", session="desk", desktop=True)  # Desktop: a's
                + hit(first_hit, "seven_day", week)
                + reply(first_hit + timedelta(minutes=2), output=MILLION, msg="tail")    # was under way at the hit
                + hit(first_hit + timedelta(minutes=3), "seven_day", week)
                + reply(now - 10 * hour, output=10 * MILLION, msg="a2")                  # the window started over
                + reply(now - 9 * hour, output=MILLION, msg="a3", model="claude-fable-5-1")
                + reply(now - 8 * hour, output=5 * MILLION, msg="c1", session="child", desktop=True))  # as cswap: a
            reading = [self.account("a@example.com", now - hour, **{"7d": (20, week), "Fable": (10, week)})]
            worth.observe(reading, (now - hour).timestamp())
            result = worth.measure(reading, now.timestamp())
        rows = {(r["window"], r["segment"]): r for r in result["rows"] if r["email"] == "a@example.com"}
        before, after, fable = rows[("7d", 1)], rows[("7d", 2)], rows[("Fable", 2)]
        self.assertEqual((before["method"], before["pct"], before["of"]), ("упор", 100.0, 2))
        self.assertAlmostEqual(before["full"]["usd"], 55 * OPUS_OUT, delta=0.1)       # a1 and Desktop's d1, not b1 or f1
        self.assertEqual((after["method"], after["from_kind"], after["points"]), ("уровень", "restart", 20.0))
        self.assertAlmostEqual(after["from"], (now - 10 * hour).timestamp())
        self.assertAlmostEqual(after["spent"]["usd"], 15 * OPUS_OUT + 50, delta=0.1)   # a2, c1 and Fable's a3
        self.assertAlmostEqual(after["spent"]["usd_alone"], 5 * OPUS_OUT, delta=0.1)
        self.assertAlmostEqual(after["full"]["usd"], (15 * OPUS_OUT + 50) / 20 * 100, delta=1)
        self.assertLess(after["full"]["low"], after["full"]["usd"])
        self.assertAlmostEqual(fable["full"]["usd"], 50 / 10 * 100, delta=1)           # only Fable counts there
        self.assertEqual(result["data"]["checks"]["matched"], 2)
        self.assertEqual([round(r["at"]) for r in result["data"]["restarts"]], [round((now - 10 * hour).timestamp())])

    def test_readings_measure_by_level_or_by_their_difference(self):
        now, hour = self.NOW, timedelta(hours=1)
        week = now + timedelta(days=4)
        with contextlib.ExitStack() as stack:
            home = self.home(stack)
            (home / ".claude-swap-backup/claude-swap.log").write_text(
                log_line(now - 200 * hour, "Added account 1: a@example.com (org: org-a)"))
            (home / ".claude/projects/p/t.jsonl").write_text(
                reply(now - 60 * hour, output=10 * MILLION, msg="unseen")                # before anyone watched
                + reply(now - 30 * hour, output=10 * MILLION, msg="seen")
                + reply(now - 40 * 60 * timedelta(seconds=1), output=2 * MILLION, msg="late", stop=None))
            for hours, pct in ((40, 30), (20, 55), (1, 60)):
                at = now - hours * hour
                reading = [self.account("a@example.com", at, **{"7d": (pct, week), "5h": (50 if hours == 1 else 0, now + 2 * hour if hours == 1 else None)})]
                worth.observe(reading, at.timestamp())
            result = worth.measure(reading, now.timestamp())
        rows = {r["window"]: r for r in result["rows"]}
        weekly, five = rows["7d"], rows["5h"]
        self.assertEqual((weekly["method"], weekly["points"]), ("разность", 30.0))       # 30% → 60%, start unwatched
        self.assertAlmostEqual(weekly["full"]["usd"], 10 * OPUS_OUT / 30 * 100, delta=0.1)
        self.assertAlmostEqual(weekly["full"]["low"], 10 * OPUS_OUT / 31 * 100, delta=0.1)
        self.assertAlmostEqual(weekly["full"]["high"], 10 * OPUS_OUT / 29 * 100, delta=0.1)
        # Spend stops at the reading: the reply 40 minutes ago came after the percentage was read.
        self.assertEqual((five["method"], five["points"], five["spent"]["usd"]), ("уровень", 50.0, 0))
        self.assertIsNone(five["full"])

    def test_output_is_estimated_from_content_when_enough_replies_show_how(self):
        now = self.NOW
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "subagents"
            root.mkdir()
            lines = [reply(now - timedelta(minutes=n), output=10 + 2 * n, msg=f"m{n}",
                           block={"type": "text", "text": "x" * (4 * n)}) for n in range(600)]
            (root / "agent.jsonl").write_text("".join(lines))
            db = worth.open_index(Path(tmp) / "index.sqlite3")
            worth.update_index(db, root, now.timestamp())
            b0, text, args, signature, r2, n = worth.output_models(db, now.timestamp())["claude-opus-5-5"]
            self.assertAlmostEqual(b0, 10, delta=0.01)
            self.assertAlmostEqual(text, 0.5, delta=0.001)       # two tokens per four characters
            self.assertGreater(r2, 0.99)
            db.execute("DELETE FROM replies WHERE ts < ?", ((now - timedelta(minutes=100)).timestamp(),))
            mean, *slopes, r2, n = worth.output_models(db, now.timestamp())["claude-opus-5-5"]
            self.assertEqual((slopes, r2, n), ([0.0, 0.0, 0.0], None, 101))  # too few: the model's mean stands in
            self.assertAlmostEqual(mean, 10 + 2 * 50)

    ROW = {"email": "alice@example.com", "window": "7d", "resets_at": 1791000000.0, "segment": 2, "of": 2,
           "from": 1790700000.0, "from_kind": "restart", "until": 1790900000.0, "pct": 86.0, "points": 86.0,
           "method": "уровень", "plan": "Max 20x", "current": True, "nobody_usd": 0.0, "flags": [],
           "spent": {"usd": 2045.0, "usd_read": 1040.0, "usd_alone": 400.0, "usd_guessed": 20.0, "usd_edge": 0.0,
                     "t_in": 0, "t_out": 16_000_000, "t_read": 5_200_000_000, "t_write": 95_000_000, "guessed": 0,
                     "models": {"claude-opus-5-5": 1800.0, "claude-fable-5-1": 240.0, "claude-haiku-4-5-20251001": 5.0}},
           "full": {"usd": 2378.0, "low": 2350.0, "high": 2406.0, "no_read_usd": 1169.0, "tokens": 6.2e9, "rough": False}}
    DATA = {"now": 1790900000.0, "indexed": {"from": 1783000000.0, "to": 1790900000.0, "replies": 369467},
            "log_from": 1790000000.0, "usd": 18700.0, "usd_guessed": 1222.0, "usd_alone": 1749.0, "usd_edge": 226.0,
            "usd_nobody": 102.0, "fits": {"claude-opus-5-5": {"r2": 0.92, "n": 99796}, "claude-fable-5": {"r2": None, "n": 51200}},
            "unpriced": {"claude-opus-6": 3}, "checks": {"matched": 187, "mismatched": [], "alone_matched": 16, "alone_mismatched": 0},
            "restarts": [{"email": "alice@example.com", "at": 1790700000.0}],
            "plans": {"alice@example.com": [(1790600000.0, "Max 5x"), (1790800000.0, "Max 20x")]}, "coverage": {"2026-10-03": 5}}

    def test_statistics_tabs_show_the_segments_and_what_they_rest_on(self):
        exact = dict(self.ROW, segment=1, method="упор", pct=100.0, current=False, from_kind="reset", plan="Max 20x?",
                     full=dict(self.ROW["full"], usd=2236.0, no_read_usd=1409.0))
        five = dict(exact, window="5h", of=1, flags=["начато не здесь"])
        result = {"rows": [exact, self.ROW, five], "data": self.DATA}
        now, weeks, hours, data = (watch.ANSI.sub("", "\n".join(stats.tab_lines(result, n))) for n in range(4))
        self.assertRegex(now, r"alice +7d +86% +↺ \d\d\.\d\d \d\d:\d\d +\$2,045 +\$23\.8 +\$2,378 \(2,350–2,406\) +\$1,169 +уровень")
        self.assertIn("неделя · Max 20x", weeks)
        self.assertRegex(weeks, r"alice +100% +\$2,045 +\$2,236 +\$1,409 +упор +51% +отр\. 1/2 · Desktop\? 20% · тариф\?")
        self.assertRegex(weeks, r"отр\. 2/2 · идёт · Desktop\? 20% +Opus 5\.5 88% · Fable 5\.1 12% +111M \+ 5\.2B")
        self.assertIn("точных замеров (упор): 1 · по API $2,236 · без чтения кэша $1,409", weeks)
        self.assertIn("5 часов · Max 20x", hours)
        self.assertNotIn("точных замеров", hours)   # a window opened elsewhere is shown, not summed up
        for text in ("369 467 ответов", "выход оценён по содержимому ответа: $1,222 (6.5%)", "совпало 16 · не совпало 0",
                     "alice: Max 5x", "→ Max 20x", "Opus 5.5: по содержимому, R² 0.92 · 99 796 ответов",
                     "Fable 5: среднее по модели", "claude-opus-6 (3)"):
            self.assertIn(text, data)
        frame, scroll = stats.screen(result, 1, 99, 80, 12)
        lines = watch.ANSI.sub("", frame).splitlines()
        self.assertTrue(all(len(line) < 80 for line in lines))
        self.assertLessEqual(len(lines), 12)
        self.assertIn("2 Недели", lines[0])
        self.assertGreater(scroll, 0)   # scrolling stops where the table ends
        self.assertIn("считаю расход", stats.screen(None, 0, 0, 100, 30)[0])
        self.assertEqual(json.loads(stats.as_json(result))["rows"][0]["resets_at"], "2026-10-03T04:00:00+00:00")
        self.assertIn("НЕДЕЛИ", stats.report(result))

    def test_amounts_and_models_are_short(self):
        self.assertEqual([stats.money(x) for x in (1.934, 23.78, 2378.4, 51130)], ["$1.93", "$23.8", "$2,378", "$51.1k"])
        self.assertEqual([stats.tokens(x) for x in (293_000, 5_400_000, 62_000_000, 6_180_000_000)],
                         ["293K", "5.4M", "62M", "6.2B"])
        self.assertEqual([stats.model_name(m) for m in ("claude-opus-5-5", "claude-haiku-4-5-20251001",
                                                        "claude-fable-5-1[1m]", "claude-sonnet-5")],
                         ["Opus 5.5", "Haiku 4.5", "Fable 5.1", "Sonnet 5"])
        self.assertEqual(stats.model_shares({"claude-fable-5-1": 11.0, "claude-opus-5-5": 88.5, "claude-haiku-4-5": 0.5}),
                         "Opus 5.5 88% · Fable 5.1 11%")
        self.assertEqual(stats.model_shares({"claude-opus-5-5": 3.0}), "Opus 5.5")


class Live(unittest.TestCase):
    CLAUDE = [dict(provider="claude", email=f"{name}@example.com", slot=slot, active=slot == 2,
                   windows=[{"pct": 10 * slot, "label": "7d"}])
              for slot, name in ((1, "alice"), (2, "carol"), (3, "dave"))]

    def test_keys_are_read_in_both_layouts(self):
        self.assertEqual(watch.keys(b"\x1b[A\x1bOB3\rx"), ["up", "down", "3", "enter"])
        self.assertEqual(watch.keys("кй".encode()), ["refresh", "quit"])
        self.assertEqual(watch.keys("sы".encode() + b"\x1b[C\x1b[D"), ["stats", "stats", "right", "left"])
        self.assertEqual(watch.keys(b"\x1b"), ["escape"])

    def test_selection_starts_next_to_the_active_account(self):
        move = watch.move
        self.assertEqual(move(None, "down", self.CLAUDE), 3)
        self.assertEqual(move(None, "up", self.CLAUDE), 1)
        self.assertEqual(move(3, "down", self.CLAUDE), 3)
        self.assertEqual(move(None, "1", self.CLAUDE), 1)
        self.assertEqual(move(1, "7", self.CLAUDE), 1)
        self.assertIsNone(move(3, "escape", self.CLAUDE))
        self.assertIsNone(move(None, "down", None))

    def test_selected_account_is_marked_and_hinted(self):
        with patch.object(watch.shutil, "get_terminal_size", return_value=os.terminal_size((100, 40))):
            text = watch.render(self.CLAUDE, [], 0, 0, [], cursor=3, status="Enter — переключиться на 3 dave",
                                interactive=True)
        lines = watch.ANSI.sub("", text).splitlines()
        self.assertTrue(next(l for l in lines if "dave" in l).startswith("▶ 3  dave"))
        self.assertTrue(next(l for l in lines if "alice" in l).startswith("  1  alice"))
        self.assertIn(watch.SELECT, next(l for l in text.splitlines() if "dave" in l))
        self.assertIn("Enter — переключиться на 3 dave", text)
        self.assertIn("↑↓ выбрать", lines[-1])
        with patch.object(watch.shutil, "get_terminal_size", return_value=os.terminal_size((100, 40))):
            self.assertIn("cswap switch <номер>", watch.ANSI.sub("", watch.render(self.CLAUDE, [], 0, 0, [])))

    def test_codex_cache_roundtrip_is_private_and_rejects_junk(self):
        acc = dict(provider="codex", email="bob@example.com", windows=[{"label": "7d", "pct": 40.0}])
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"XDG_CACHE_HOME": tmp}):
            self.assertIsNone(watch.load_codex_cache())
            watch.save_codex_cache([acc])
            self.assertEqual(watch.load_codex_cache(), [acc])
            self.assertEqual(os.stat(watch.cache_path()).st_mode & 0o777, 0o600)
            for junk in ("broken", "[]", json.dumps({"accounts": [{"provider": "claude"}]})):
                watch.cache_path().write_text(junk)
                self.assertIsNone(watch.load_codex_cache())

    def test_switch_goes_through_cswap_json(self):
        calls = []
        def answer(out):
            def fake(cmd, timeout=180, env=None):
                calls.append(cmd)
                return out
            return fake
        ok = {"switched": True, "to": {"number": 3, "email": "dave@example.com"}}
        with patch.object(watch, "run", side_effect=answer(json.dumps(ok))):
            self.assertEqual(watch.switch_account(3), ok)
        self.assertEqual(calls[0][1:], ["switch", "3", "--json"])
        self.assertIn("Переключено на 3 dave", watch.switch_note(ok, None))
        envelope = {"error": {"message": "Account 9 not found"}}
        for out, message in ((json.dumps(envelope), "cswap: Account 9 not found"), ("", "cswap switch не ответил")):
            with patch.object(watch, "run", side_effect=answer(out)), self.assertRaises(watch.FetchError) as caught:
                watch.switch_account(9)
            self.assertEqual(str(caught.exception), message)

    def test_children_never_read_the_terminal(self):
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as spawned:
            watch.run(["test"])
        self.assertIs(spawned.call_args.kwargs["stdin"], subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
