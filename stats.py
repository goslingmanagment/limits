"""The statistics screen: what the Claude limits were worth, window by window and week by week.

Shows what worth.measure() derived; nothing here measures anything.
"""

import json
import re
import time
from datetime import datetime, timezone

RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
GREEN, YELLOW, RED, ORANGE, CYAN = "\033[32m", "\033[33m", "\033[31m", "\033[38;5;214m", "\033[36m"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
TABS = ("Сейчас", "Недели", "5 часов", "Данные")
SHOWN_5H = 15  # five-hour windows pile up; the latest of each plan say enough
LEGEND = ("за 100% — цена по API при такой же работе, в скобках диапазон · тариф? — по более позднему наблюдению",
          "без чт. кэша — та же цена без чтения кэша: недели лучше сравнивать по ней",
          "Desktop? — доля запусков из Desktop без его записи (отнесены по cswap) · выход≈ — доля оценённого выхода")


def visible(text):
    return len(ANSI.sub("", text))


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


def clock(ts, year=False):
    return time.strftime("%d.%m.%Y %H:%M" if year else "%d.%m %H:%M", time.localtime(ts))


def name(email):
    return email.split("@")[0]


def model_name(model):
    """claude-opus-5-5 -> Opus 5.5, claude-haiku-4-5-20251001 -> Haiku 4.5."""
    parts = model.split("[")[0].casefold().replace("claude-", "", 1).split("-")
    version = ".".join(part for part in parts[1:] if part.isdigit() and len(part) < 4)
    return f"{parts[0].capitalize()} {version}".strip()


def model_shares(models):
    """Where the money went: 'Opus 5.5 87% · Fable 5.1 11%'; a single model is just named."""
    total = sum(models.values())
    ranked = sorted(models.items(), key=lambda item: -item[1])
    if total <= 0:
        return ""
    if len(ranked) == 1:
        return model_name(ranked[0][0])
    return " · ".join(f"{model_name(model)} {usd / total * 100:.0f}%" for model, usd in ranked if usd >= total / 100)


def table(head, rows, right=()):
    """Aligned lines: a dim header, then the rows; columns named in `right` align to the right."""
    widths = [max(visible(str(row[i])) for row in [head] + rows) for i in range(len(head))]
    def line(cells):
        return "  ".join(" " * (w - visible(c)) + c if i in right else c + " " * (w - visible(c))
                          for i, (c, w) in enumerate(zip(map(str, cells), widths))).rstrip()
    return [f"  {DIM}{line(head)}{RESET}"] + ["  " + line(row) for row in rows]


def full_cell(row):
    """What 100% comes to: exact at a hit, a range otherwise."""
    full = row["full"]
    if not full:
        return f"{DIM}—{RESET}"
    if row["method"] == "упор":
        return f"{BOLD}{money(full['usd'])}{RESET}"
    high = money(full["high"])[1:] if full["high"] else "?"
    text = f"{money(full['usd'])} {DIM}({money(full['low'])[1:]}–{high}){RESET}"
    return f"{DIM}~{RESET}{text}" if full["rough"] else text


def marks(row):
    """Short notes on a row: its place among the segments and what part of the spend is uncertain."""
    spent, notes = row["spent"], []
    if row["of"] > 1:
        notes.append(f"отр. {row['segment']}/{row['of']}")
    if row["current"] and row["method"] != "упор":
        notes.append("идёт")
    if spent["usd"] > 0:
        for part, label in (("usd_alone", "Desktop?"), ("usd_guessed", "выход≈")):
            if spent[part] >= spent["usd"] * 0.05:
                notes.append(f"{label} {spent[part] / spent['usd'] * 100:.0f}%")
    if row["plan"].endswith("?"):
        notes.append("тариф?")
    return " · ".join(notes + row["flags"])


def since_cell(row):
    return ("↺ " if row["from_kind"] == "restart" else "") + clock(row["from"])


def now_tab(result):
    rows, lines = [r for r in result["rows"] if r["current"]], []
    order = {"5h": 0, "7d": 1}
    accounts = sorted({r["email"] for r in rows})
    body = []
    for email in accounts:
        mine = sorted((r for r in rows if r["email"] == email), key=lambda r: (order.get(r["window"], 2), r["window"]))
        for n, r in enumerate(mine):
            full = r["full"]
            body.append((
                f"{BOLD}{name(email)}{RESET}" if n == 0 else f"{DIM}{r['plan']}{RESET}" if n == 1 else "",
                r["window"], f"{r['pct']:.0f}%", since_cell(r), money(r["spent"]["usd"]),
                money(full["usd"] / 100) if full else f"{DIM}—{RESET}", full_cell(r),
                money(full["no_read_usd"]) if full else f"{DIM}—{RESET}", r["method"] if full else "",
                f"{DIM}{model_shares(r['spent']['models'])}{RESET}"))
        if len(mine) == 1:
            body.append((f"{DIM}{mine[0]['plan']}{RESET}",) + ("",) * 9)
    if not body:
        return [f"  {DIM}нет идущих окон с замером{RESET}"]
    lines += table(("аккаунт", "окно", "%", "считаем с", "потрачено", "за 1%", "за 100%", "без чт. кэша", "способ",
                    "модели"), body, right=(2, 3))
    return lines + [""] + [f"  {DIM}{line}{RESET}" for line in LEGEND + ("↺ — процент начался заново посреди окна",)]


def spread(values):
    return f"{money(min(values))}–{money(max(values))}" if len(values) > 1 else money(values[0])


def group_tab(result, five):
    """Tables by plan for weekly windows (or five-hour ones), oldest first, with a summary of exact measurements."""
    groups = {}
    for r in result["rows"]:
        if (r["window"] == "5h") != five or not r["full"]:
            continue
        if five and r["method"] != "упор" and r["pct"] < 30:
            continue
        groups.setdefault((r["window"] != "7d", r["window"], r["plan"].rstrip("?")), []).append(r)
    lines = []
    for (_, label, plan), rows in sorted(groups.items()):
        rows.sort(key=lambda r: (r["resets_at"], r["segment"]))
        rows = rows[-SHOWN_5H:] if five else rows
        body = []
        for r in rows:
            spent, full = r["spent"], r["full"]
            other = spent["t_in"] + spent["t_out"] + spent["t_write"]
            body.append((clock(r["resets_at"]), name(r["email"]), f"{r['pct']:.0f}%", money(spent["usd"]), full_cell(r),
                         money(full["no_read_usd"]), r["method"], f"{spent['usd_read'] / spent['usd'] * 100:.0f}%",
                         f"{YELLOW}{marks(r)}{RESET}", f"{DIM}{model_shares(spent['models'])}{RESET}",
                         f"{tokens(other)} + {tokens(spent['t_read'])}"))
        lines += ["", f"  {BOLD}{'5 часов' if five else 'неделя' if label == '7d' else label} · "
                      f"{plan or 'тариф неизвестен'}{RESET}"]
        lines += table(("окно до", "аккаунт", "%", "потрачено", "за 100% по API", "без чт. кэша", "способ", "кэш",
                        "метки", "модели", "токены + чт. кэша"), body, right=(2, 7))
        exact = [r["full"] for r in rows if r["method"] == "упор" and not r["flags"]]
        if exact:
            lines.append(f"  {DIM}точных замеров (упор): {len(exact)} · по API {spread([f['usd'] for f in exact])}"
                         f" · без чтения кэша {spread([f['no_read_usd'] for f in exact])}{RESET}")
    if not lines:
        return [f"  {DIM}замеров пока нет{RESET}"]
    return lines[1:] + [""] + [f"  {DIM}{line}{RESET}" for line in LEGEND]


def share(part, whole):
    return f"{money(part)} ({part / whole * 100:.1f}%)" if whole > 0 else money(part)


def data_tab(result):
    d, lines = result["data"], []
    indexed, checks = d["indexed"], d["checks"]
    lines.append(f"  {BOLD}На чём стоят цифры{RESET}")
    if indexed["replies"]:
        lines.append(f"  транскрипты: {indexed['replies']:,} ответов с {clock(indexed['from'], True)} "
                     f"по {clock(indexed['to'])}".replace(",", " "))
    if d["log_from"]:
        lines.append(f"  журнал cswap с {clock(d['log_from'], True)}: расход до этого ни к кому не отнесён и в таблицы не входит")
    usd = d["usd"]
    lines += ["", f"  {BOLD}Расход с начала журнала: {money(usd)}{RESET}",
              f"  выход оценён по содержимому ответа: {share(d['usd_guessed'], usd)}",
              f"  запуски из Desktop без его записи (считаются по аккаунту cswap): {share(d['usd_alone'], usd)}",
              f"  первые 60 секунд после переключения cswap: {share(d['usd_edge'], usd)}",
              f"  аккаунт неизвестен (вход сменили мимо cswap): {share(d['usd_nobody'], usd)}"]
    lines += ["", f"  {BOLD}Сверка аккаунта{RESET} {DIM}(время сброса в сообщении об упоре называет аккаунт){RESET}",
              f"  совпало с журналом cswap и записями Desktop: {checks['matched']} · "
              f"не совпало: {len(checks['mismatched'])}",
              f"  из них запуски из Desktop без записи: совпало {checks['alone_matched']} · "
              f"не совпало {checks['alone_mismatched']}"]
    for miss in checks["mismatched"][-5:]:
        lines.append(f"    {RED}{clock(miss['at'])} {miss['window']}: по журналу {name(miss['by_log'])}, "
                     f"по сбросу {name(miss['by_reset'])}{RESET}")
    lines += ["", f"  {BOLD}Перезапуски процента{RESET} {DIM}(смена тарифа или сброс лимитов; причина неизвестна){RESET}"]
    lines += [f"  {clock(r['at'], True)}   {name(r['email'])}" for r in d["restarts"]] or [f"  {DIM}не было{RESET}"]
    lines += ["", f"  {BOLD}Тарифы по наблюдениям{RESET}"]
    for email, series in sorted(d["plans"].items()):
        lines.append(f"  {name(email)}: " + " → ".join(f"{plan} ({clock(at)})" for at, plan in series))
    lines += ["", f"  {BOLD}Ряд процентов{RESET} {DIM}(часов в сутках с замерами; пишется, пока открыт limits){RESET}"]
    days = list(d["coverage"].items())[-7:]
    lines.append("  " + ("   ".join(f"{day[8:]}.{day[5:7]}: {hours} ч" for day, hours in days) or f"{DIM}замеров нет{RESET}"))
    lines += ["", f"  {BOLD}Оценка выхода ответов без финальной копии{RESET}"]
    for model, fit in sorted(d["fits"].items()):
        how = f"по содержимому, R² {fit['r2']:.2f}" if fit["r2"] else "среднее по модели"
        lines.append(f"  {model_name(model)}: {how} · " + f"{fit['n']:,} ответов".replace(",", " "))
    if d["unpriced"]:
        lines += ["", f"  {RED}модели без цены, в расход не входят: " + ", ".join(
            f"{model} ({n})" for model, n in sorted(d["unpriced"].items())) + RESET]
    return lines


def tab_lines(result, tab):
    return (now_tab, lambda r: group_tab(r, False), lambda r: group_tab(r, True), data_tab)[tab](result)


def clip(line, width):
    """Clip visible cells, keeping escape sequences whole."""
    parts, used = [], 0
    for part in re.split(r"(\x1b\[[0-9;]*m)", line):
        if ANSI.fullmatch(part):
            parts.append(part)
        elif used < width:
            parts.append(part[:width - used])
            used += len(part[:width - used])
    return "".join(parts) + RESET


def screen(result, tab, scroll, columns, rows, status=""):
    """The live statistics screen: tabs, the part of the table that fits, a status and a hint."""
    width = max(1, columns - 1)
    head = "  " + f"{BOLD}СТАТИСТИКА{RESET}   " + f"{DIM} · {RESET}".join(
        f"{BOLD}{CYAN}{n + 1} {title}{RESET}" if n == tab else f"{DIM}{n + 1} {title}{RESET}"
        for n, title in enumerate(TABS))
    body = tab_lines(result, tab) if result else [f"  {DIM}считаю расход по транскриптам…{RESET}"]
    room = max(1, rows - 5)
    scroll = max(0, min(scroll, len(body) - room))
    shown = body[scroll:scroll + room]
    more = f"   ·   ещё {len(body) - scroll - room} ниже" if len(body) > scroll + room else ""
    counted = f"   ·   посчитано {time.strftime('%H:%M', time.localtime(result['data']['now']))}" if result else ""
    foot = ["", *([f"  {status}"] if status else []),
            f"  {DIM}1–4 вкладка · ↑↓ прокрутка · r пересчитать · s назад · q выход{counted}{more}{RESET}"]
    return "\n".join(clip(line, width) for line in [head, ""] + shown + foot), scroll


def report(result):
    """Every tab one after another, for `limits --stats`."""
    lines = []
    for n, title in enumerate(TABS):
        lines += ["", f"{BOLD}{title.upper()}{RESET}", ""] + tab_lines(result, n)
    return "\n".join(lines[1:])


def as_json(result):
    """The measured segments and what they rest on, with times as ISO strings."""
    def iso(ts):
        return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None
    rows = [dict(row, resets_at=iso(row["resets_at"]), until=iso(row["until"]), **{"from": iso(row["from"])})
            for row in result["rows"]]
    d = result["data"]
    data = dict(d, now=iso(d["now"]), log_from=iso(d["log_from"]),
                indexed=dict(d["indexed"], **{"from": iso(d["indexed"]["from"]), "to": iso(d["indexed"]["to"])}),
                restarts=[dict(r, at=iso(r["at"])) for r in d["restarts"]],
                plans={email: [[iso(at), plan] for at, plan in series] for email, series in d["plans"].items()},
                checks=dict(d["checks"], mismatched=[dict(m, at=iso(m["at"])) for m in d["checks"]["mismatched"]]))
    return json.dumps({"rows": rows, "data": data}, ensure_ascii=False, indent=2)
