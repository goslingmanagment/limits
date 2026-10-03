import base64
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
import worth
import watch


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



def reply(at, model="claude-opus-5-5", session="cli", output=100, cache_read=0, msg="m1", desktop=False):
    """One transcript line of an assistant reply, as Claude Code writes it."""
    return json.dumps({
        "type": "assistant", "timestamp": at.isoformat().replace("+00:00", "Z"), "requestId": "r-" + msg,
        "sessionId": session, "entrypoint": "claude-desktop" if desktop else "cli", "uuid": "u-" + msg,
        "message": {"id": msg, "model": model, "usage": {
            "input_tokens": 1000, "output_tokens": output, "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}},
    }) + "\n"


def log_line(at, text):
    return datetime.fromtimestamp(at.timestamp()).strftime("%Y-%m-%d %H:%M:%S,000") + " - INFO - " + text + "\n"


class Cost(unittest.TestCase):
    NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)

    def test_streamed_copies_count_once_and_unfinished_lines_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, at = Path(tmp) / "projects", self.NOW - timedelta(hours=1)
            root.mkdir()
            transcript = root / "s.jsonl"
            done = reply(at, output=8) + reply(at, output=120) + "{broken\n"
            transcript.write_text(done + reply(at, msg="m2")[:40])
            db = worth.open_index(Path(tmp) / "index.sqlite3")
            worth.update_index(db, root, self.NOW.timestamp())
            self.assertEqual(db.execute("SELECT key, output FROM replies").fetchall(), [("m1:r-m1", 120)])
            self.assertEqual(db.execute("SELECT offset FROM files").fetchone()[0], len(done.encode()))
            transcript.write_text(done + reply(at, msg="m2"))
            worth.update_index(db, root, self.NOW.timestamp())
            self.assertEqual(db.execute("SELECT count(*) FROM replies").fetchone()[0], 2)
            transcript.write_text(reply(at, msg="m3"))  # rewritten shorter: read from the start
            worth.update_index(db, root, self.NOW.timestamp())
            self.assertEqual(db.execute("SELECT count(*) FROM replies").fetchone()[0], 3)
            old = (self.NOW + timedelta(days=9)).timestamp()
            worth.update_index(db, root, old)
            self.assertEqual(db.execute("SELECT count(*) FROM replies").fetchone()[0], 0)
            self.assertEqual(os.stat(Path(tmp) / "index.sqlite3").st_mode & 0o777, 0o600)

    def test_prices_follow_the_model_and_cache_tier(self):
        million = 1_000_000
        self.assertEqual(worth.price("claude-opus-5-5", (million, million, million, 0, 0)), 4 + 20 + 0.2)
        self.assertEqual(worth.price("claude-fable-5-1", (0, 0, million, million, million)), 0.25 + 12.5 + 20)
        self.assertEqual(worth.price("claude-sonnet-5-5", (million, 0, 0, 0, 0), fast=True), 4)
        self.assertEqual(worth.price("claude-opus-6", (million, 0, 0, 0, 0)), 4)
        self.assertEqual(worth.price("<synthetic>", (million, 0, 0, 0, 0)), 0)

    def test_cswap_log_follows_slots_and_unknown_credentials(self):
        t = [self.NOW + timedelta(minutes=i) for i in range(8)]
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
                + log_line(t[7], "Resynced account 5's backup to the rotated live credential"))
            history = worth.account_history(tmp)
        self.assertEqual(history, [(t[0].timestamp(), "a@example.com"), (t[2].timestamp(), "b@example.com"),
                                   (t[4].timestamp(), "a@example.com"), (t[5].timestamp(), None),
                                   (t[7].timestamp(), "a@example.com")])

    def test_usage_goes_to_the_account_it_ran_as(self):
        now = self.NOW
        hour = timedelta(hours=1)
        accounts = [
            dict(provider="claude", email="a@example.com", org="org-a", windows=[
                {"label": "7d", "pct": 50.0, "resets_at": (now + timedelta(days=6)).isoformat()},
                {"label": "Fable", "scope": "Fable", "pct": 10.0, "resets_at": (now + timedelta(days=6)).isoformat()}]),
            dict(provider="claude", email="b@example.com", org="org-b", windows=[
                {"label": "5h", "pct": 20.0, "resets_at": (now + 4 * hour).isoformat()}]),
        ]
        with tempfile.TemporaryDirectory() as tmp, patch("pathlib.Path.home", return_value=Path(tmp)), \
                patch.dict(os.environ, {"XDG_CACHE_HOME": tmp + "/cache"}), patch.dict(os.environ) as env:
            env.pop("CLAUDE_CONFIG_DIR", None)
            home = Path(tmp)
            (home / ".claude-swap-backup").mkdir()
            (home / ".claude-swap-backup/claude-swap.log").write_text(
                log_line(now - 10 * hour, "Added account 1: a@example.com (org: org-a)")
                + log_line(now - 2 * hour, "Added account 2: b@example.com (org: org-b)"))
            record = home / "Library/Application Support/Claude/claude-code-sessions/user-a/org-a/local_1.json"
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps({"cliSessionId": "desk"}))
            projects = home / ".claude/projects/p"
            projects.mkdir(parents=True)
            (projects / "t.jsonl").write_text(
                reply(now - 20 * hour, msg="before")                    # no account known yet
                + reply(now - 5 * hour, msg="a1")                       # CLI as a
                + reply(now - 5 * hour, model="claude-fable-5-1", output=0, msg="a2")
                + reply(now - hour / 2, msg="b1")                       # CLI as b, inside b's 5h window
                + reply(now - hour / 2, session="desk", desktop=True, msg="d1")    # Desktop session of a
                + reply(now - hour / 2, session="child", desktop=True, msg="c1"))  # launched from Desktop: as b
            spend = worth.window_spend(accounts, now.timestamp())
        week, fable, five = spend[("a@example.com", "7d")], spend[("a@example.com", "Fable")], spend[("b@example.com", "5h")]
        opus = worth.price("claude-opus-5-5", (1000, 100, 0, 0, 0))
        self.assertAlmostEqual(week["usd"], 2 * opus + worth.price("claude-fable-5-1", (1000, 0, 0, 0, 0)))
        self.assertAlmostEqual(week["unattributed_usd"], opus)
        self.assertEqual(week["tokens"], {"input": 3000, "output": 200, "cache_read": 0, "cache_write": 0})
        self.assertEqual(fable["tokens"]["input"], 1000)
        self.assertAlmostEqual(five["usd"], 2 * opus)
        self.assertIs(worth.lookup(spend, "b@example.com", accounts[1]["windows"][0]), five)
        restarted = dict(accounts[1]["windows"][0], resets_at=(now + 5 * hour).isoformat())
        self.assertIsNone(worth.lookup(spend, "b@example.com", restarted))

    def test_estimate_needs_a_percent_and_local_spend(self):
        spent = {"usd": 50.0, "tokens": {"input": 1, "output": 2, "cache_read": 997, "cache_write": 0},
                 "models": {}, "unattributed_usd": 0.0}
        self.assertEqual(worth.estimate(spent, 25), {"usd": 2.0, "tokens": 40.0, "rough": False})
        self.assertTrue(worth.estimate(spent, 5)["rough"])
        self.assertIsNone(worth.estimate(spent, 0))
        self.assertIsNone(worth.estimate(dict(spent, usd=0.0), 30))
        self.assertEqual(worth.summary(spent, 25)["full"], {"usd": 200.0, "tokens": 4000})

    def test_cost_view_replaces_bars_with_prices(self):
        reset = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        windows = [{"label": "7d", "pct": 86.0, "resets_at": reset}, {"label": "5h", "pct": 4.0, "resets_at": reset}]
        claude = [dict(provider="claude", email="alice@example.com", slot=1, active=True, windows=windows)]
        codex = [dict(provider="codex", email="bob@example.com", active=False,
                      windows=[{"label": "7d", "pct": 20.0, "resets_at": reset}])]
        spent = {"usd": 2045.0, "tokens": {"input": 0, "output": 16_000_000, "cache_read": 5_200_000_000,
                                           "cache_write": 95_000_000}, "unattributed_usd": 0.0,
                 "models": {"claude-opus-5-5": {"usd": 1800.0}, "claude-fable-5-1": {"usd": 240.0},
                            "claude-haiku-4-5-20251001": {"usd": 5.0}}}
        spend = {("alice@example.com", label): dict(spent, start=worth.window_start(w))
                 for label, w in (("7d", windows[0]), ("5h", windows[1]))}
        with patch.object(watch.shutil, "get_terminal_size", return_value=os.terminal_size((120, 40))):
            lines = watch.ANSI.sub("", watch.render(claude, codex, 0, 0, [], cost=True, spend=spend,
                                                    interactive=True)).splitlines()
        self.assertIn("за 100%", lines[0])
        self.assertIn("модели", lines[0])
        self.assertTrue(next(l for l in lines if "86%" in l).endswith("Opus 5.5 88% · Fable 5.1 12%"))
        self.assertRegex(next(l for l in lines if "86%" in l), r"86%  \$23\.8 · 62M +\$2,378 · 6\.2B +2d")
        self.assertRegex(next(l for l in lines if " 4%" in l), r"4%  ~\$511 · 1\.3B +~\$51\.1k · 133B")
        self.assertIn("—", next(l for l in lines if "bob" in l))
        self.assertIn("c лимиты", lines[-1])
        self.assertFalse(any("━" in line for line in lines))
        with patch.object(watch.shutil, "get_terminal_size", return_value=os.terminal_size((70, 40))):
            lines = watch.ANSI.sub("", watch.render(claude, codex, 0, 0, [], cost=True, spend=spend)).splitlines()
        self.assertTrue(all(len(line) < 70 for line in lines))
        self.assertNotIn("Opus", "\n".join(lines))
        self.assertIn("$23.8   $2,378", next(l for l in lines if "86%" in l))

    def test_models_are_named_and_shared(self):
        self.assertEqual([watch.model_name(m) for m in ("claude-opus-5-5", "claude-haiku-4-5-20251001",
                                                        "claude-fable-5-1[1m]", "claude-sonnet-5")],
                         ["Opus 5.5", "Haiku 4.5", "Fable 5.1", "Sonnet 5"])
        models = {"claude-fable-5-1": {"usd": 11.0}, "claude-opus-5-5": {"usd": 88.5}, "claude-haiku-4-5": {"usd": 0.5}}
        self.assertEqual(watch.model_shares(models, 100.0), "Opus 5.5 88% · Fable 5.1 11%")
        self.assertEqual(watch.model_shares({"claude-opus-5-5": {"usd": 3.0}}, 3.0), "Opus 5.5")
        self.assertEqual(watch.model_shares({}, 0), "")

    def test_history_keeps_each_window_course_and_its_final_estimate(self):
        now = self.NOW
        week = {"label": "7d", "pct": 40.0, "resets_at": (now + timedelta(days=2, seconds=1)).isoformat()}
        five = {"label": "5h", "pct": 40.0, "resets_at": (now + timedelta(hours=2)).isoformat()}
        account = dict(provider="claude", email="a@example.com", plan="Max 20x", windows=[week, five])
        def spend(usd):
            spent = {"usd": usd, "tokens": dict.fromkeys(worth.TOKENS, 10 ** 6), "unattributed_usd": 0.0,
                     "models": {"claude-opus-5-5": {"usd": usd, "tokens": dict.fromkeys(worth.TOKENS, 10 ** 6)}}}
            return {("a@example.com", w["label"]): dict(spent, start=worth.window_start(w)) for w in (week, five)}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"XDG_DATA_HOME": tmp}):
            self.assertEqual(worth.record_history([account], spend(400.0), now.timestamp()), 2)
            self.assertEqual(worth.record_history([account], spend(401.0), now.timestamp()), 0)
            week["pct"], five["pct"] = 41.0, 44.0  # a weekly point counts, the 5-hour window waits for 5
            self.assertEqual(worth.record_history([account], spend(410.0), now.timestamp()), 1)
            worth._written.clear()  # a new process reads where the file left off
            week["resets_at"] = (now + timedelta(days=2)).isoformat()
            self.assertEqual(worth.record_history([account], spend(410.0), now.timestamp()), 0)
            five["pct"] = 100.0
            self.assertEqual(worth.record_history([account], spend(1000.0), now.timestamp()), 1)
            path = worth.history_path()
            self.assertEqual(path, Path(tmp) / "limits/history.jsonl")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            final = worth.load_history()
        self.assertEqual([(e["window"], e["pct"], e["spent_usd"]) for e in final], [("5h", 100.0, 1000.0), ("7d", 41.0, 410.0)])
        self.assertEqual(final[1]["per_percent"]["usd"], 10.0)
        self.assertEqual(final[1]["models"]["claude-opus-5-5"]["usd"], 410.0)
        report = watch.ANSI.sub("", watch.history_report(final, now))
        self.assertLess(report.index("7d · Max 20x"), report.index("5h · Max 20x"))
        self.assertRegex(report, r"идёт   a +41%   \$10\.0 · 98K   \$1,000 · 9\.8M   Opus 5\.5")
        self.assertIn("история пуста", watch.history_report([], now))

    def test_amounts_are_short(self):
        self.assertEqual([watch.money(x) for x in (1.934, 23.78, 2378.4, 51130)], ["$1.93", "$23.8", "$2,378", "$51.1k"])
        self.assertEqual([watch.tokens(x) for x in (293_000, 5_400_000, 62_000_000, 6_180_000_000)],
                         ["293K", "5.4M", "62M", "6.2B"])


class Live(unittest.TestCase):
    CLAUDE = [dict(provider="claude", email=f"{name}@example.com", slot=slot, active=slot == 2,
                   windows=[{"pct": 10 * slot, "label": "7d"}])
              for slot, name in ((1, "alice"), (2, "carol"), (3, "dave"))]

    def test_keys_are_read_in_both_layouts(self):
        self.assertEqual(watch.keys(b"\x1b[A\x1bOB3\rx"), ["up", "down", "3", "enter"])
        self.assertEqual(watch.keys("кй".encode()), ["refresh", "quit"])
        self.assertEqual(watch.keys("cс$".encode()), ["cost", "cost", "cost"])
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
