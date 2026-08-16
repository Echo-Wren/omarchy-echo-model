#!/usr/bin/env python3
"""Collect Hermes/OpenRouter usage data for the hermes.openrouter bar widget.

Writes:
  $XDG_STATE_HOME/hermes-openrouter/stats.json        -> what Widget.qml displays
  $XDG_STATE_HOME/omarchy/agents/usage/hermes.json    -> optional omarchy.agents tab

Reads:
  $HERMES_HOME/state.db (sessions + session_model_usage) -- local usage/cost
  $HERMES_HOME/config.yaml                              -- current model
  $HERMES_HOME/.env / $OPENROUTER_API_KEY               -- API key
  https://openrouter.ai/api/v1/credits, /models         -- balance + catalogue

Never prints the API key.
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request

HOME = os.path.expanduser("~")
HERMES_HOME = os.environ.get("HERMES_HOME", os.path.join(HOME, ".hermes"))
DB = os.path.join(HERMES_HOME, "state.db")
CFG = os.path.join(HERMES_HOME, "config.yaml")
ENV = os.path.join(HERMES_HOME, ".env")
STATE_ROOT = os.environ.get("XDG_STATE_HOME", os.path.join(HOME, ".local", "state"))
OUT_DIR = os.path.join(STATE_ROOT, "hermes-openrouter")
OUT = os.path.join(OUT_DIR, "stats.json")
MODELS_CACHE = os.path.join(OUT_DIR, "models.json")
AGENTS_USAGE = os.path.join(STATE_ROOT, "omarchy", "agents", "usage")

os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(AGENTS_USAGE, exist_ok=True)

API_BASE = "https://openrouter.ai/api/v1"
MODELS_CACHE_MAX_AGE = 24 * 3600
OPENROUTER_SQL = "billing_base_url LIKE '%openrouter.ai%'"
CURATED_CAP = 14

FAVOURITES = [
    "deepseek/deepseek-v4-flash-0731",
    "deepseek/deepseek-chat",
    "deepseek/deepseek-reasoner",
    "anthropic/claude-sonnet-4.5",
    "anthropic/claude-haiku-4.5",
    "anthropic/claude-opus-4.1",
    "openai/gpt-5",
    "openai/gpt-4.1",
    "openai/gpt-4o-mini",
    "google/gemini-3-flash",
    "google/gemini-2.5-flash",
    "google/gemini-2.5-pro",
    "x-ai/grok-4",
    "x-ai/grok-4-fast",
    "qwen/qwen3-coder-480b",
    "meta-llama/llama-4-maverick",
    "mistralai/mistral-small-3.2",
    "nousresearch/hermes-4",
    "openrouter/auto",
]


# ------------------------------------------------------------------ helpers

def money(v) -> float:
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return 0.0


def int0(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(ENV) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("OPENROUTER_API_KEY="):
                    value = line.split("=", 1)[1].strip().strip("'\"").strip()
                    if value:
                        return value
    except OSError:
        pass
    return ""


def fetch_json(url: str, headers: dict, timeout: int = 20):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def db_rows(sql: str, params=()):
    if not os.path.isfile(DB):
        return []
    con = None
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, params)]
    except sqlite3.Error:
        return []
    finally:
        if con:
            con.close()


# ---------------------------------------------------------------- OpenRouter

def fetch_account(key: str):
    """(total_credits, total_usage, remaining) or None on failure."""
    if not key:
        return None
    try:
        data = fetch_json(f"{API_BASE}/credits", {"Authorization": f"Bearer {key}"})
        info = data.get("data") or {}
        total = float(info.get("total_credits") or 0)
        used = float(info.get("total_usage") or 0)
        return total, used, max(0.0, total - used)
    except Exception:
        return None


def fetch_models() -> dict:
    """{id: model} with a 24h on-disk cache so the widget never hammers it."""
    if os.path.isfile(MODELS_CACHE):
        try:
            if time.time() - os.path.getmtime(MODELS_CACHE) < MODELS_CACHE_MAX_AGE:
                with open(MODELS_CACHE) as fh:
                    data = json.load(fh)
                if isinstance(data, dict) and data:
                    return data
        except (OSError, ValueError):
            pass
    try:
        data = fetch_json(f"{API_BASE}/models", {})
        models = {
            m["id"]: m
            for m in data.get("data", [])
            if isinstance(m, dict) and m.get("id")
        }
        with open(MODELS_CACHE, "w") as fh:
            json.dump(models, fh)
        return models
    except Exception:
        try:
            with open(MODELS_CACHE) as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}


def curated_models(catalogue: dict, current: str):
    """Favourites that exist, the current model, then big-context fillers."""
    chosen, seen = [], set()

    def add(mid):
        if mid in seen or mid not in catalogue:
            return
        seen.add(mid)
        chosen.append(mid)

    for mid in FAVOURITES:
        add(mid)
    if current:
        add(current)

    if len(chosen) < CURATED_CAP:

        def is_chat(m: dict) -> bool:
            pricing = m.get("pricing") or {}
            try:
                p = float(pricing.get("prompt") or 0)
                c = float(pricing.get("completion") or 0)
            except (TypeError, ValueError):
                return False
            return (p + c) > 0

        fillers = [m for m in catalogue.values() if is_chat(m) and m["id"] not in seen]
        fillers.sort(key=lambda m: -(m.get("context_length") or 0))
        for m in fillers[: CURATED_CAP - len(chosen)]:
            add(m["id"])

    rows = []
    for mid in chosen:
        m = catalogue.get(mid, {})
        pricing = m.get("pricing") or {}
        rows.append({
            "id": mid,
            "name": m.get("name") or mid,
            "context": int0(m.get("context_length")),
            "prompt": money_model(pricing.get("prompt")),
            "completion": money_model(pricing.get("completion")),
        })
    return rows


def money_model(v):
    """Format a per-1M-token price as a compact display string."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    if f <= 0:
        return ""
    if f >= 1:
        return f"${f:.2f}"
    if f >= 0.01:
        return f"${f:.3f}".rstrip("0").rstrip(".")
    return f"${f:.5f}".rstrip("0").rstrip(".")


# --------------------------------------------------------------------- Hermes

def current_model() -> tuple:
    """(model_id, provider) parsed from config.yaml's model section."""
    if not os.path.isfile(CFG):
        return "", ""
    model, provider = "", ""
    in_model = False
    try:
        lines = open(CFG).read().splitlines()
    except OSError:
        return "", ""
    for line in lines:
        if re.match(r"^model\s*:", line):
            in_model = True
            continue
        if in_model:
            if line and line[0] not in " \t":
                break
            m = re.match(r"^\s*(default|provider)\s*:\s*([^\s#]+)", line)
            if m:
                value = m.group(2).strip("'\"")
                if m.group(1) == "default":
                    model = value
                else:
                    provider = value
    return model, provider


def by_day_rows() -> list:
    rows = db_rows(
        f"""SELECT strftime('%Y-%m-%d', datetime(first_seen,'unixepoch','localtime')) AS d,
                   SUM(input_tokens) AS inp, SUM(output_tokens) AS out,
                   SUM(cache_read_tokens) AS cache, SUM(estimated_cost_usd) AS cost
            FROM session_model_usage
            WHERE {OPENROUTER_SQL} AND first_seen >= strftime('%s','now','-6 days')
            GROUP BY d ORDER BY d"""
    )
    by = {r["d"]: r for r in rows}
    out = []
    for i in range(6, -1, -1):
        day = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
        r = by.get(day)
        tokens = int0(r["inp"]) + int0(r["out"]) + int0(r["cache"]) if r else 0
        out.append({
            "date": day,
            "tokens": tokens,
            "cost": money_model_float(r["cost"]) if r else 0.0,
        })
    return out


def money_model_float(v):
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return 0.0


def usage_summary() -> dict:
    base = (
        "SELECT SUM(input_tokens) AS inp, SUM(output_tokens) AS out, "
        "SUM(cache_read_tokens) AS cache, SUM(estimated_cost_usd) AS cost, "
        "SUM(CASE WHEN task='' THEN api_call_count ELSE 0 END) AS calls "
        f"FROM session_model_usage WHERE {OPENROUTER_SQL}"
    )
    today = db_rows(base + " AND date(first_seen,'unixepoch','localtime') = date('now','localtime')")
    week = db_rows(base + " AND first_seen >= strftime('%s','now','-7 days')")
    all_time = db_rows(base)

    def squash(rows):
        r = rows[0] if rows else {}
        return {
            "tokens": int(r.get("inp") or 0) + int(r.get("out") or 0) + int(r.get("cache") or 0),
            "cost": money_model_float(r.get("cost")),
            "calls": int(r.get("calls") or 0),
        }

    return {"today": squash(today), "week": squash(week), "allTime": squash(all_time)}


def by_model_rows(days: int = 30) -> list:
    rows = db_rows(
        f"""SELECT model AS model, SUM(input_tokens) AS inp, SUM(output_tokens) AS out,
                   SUM(cache_read_tokens) AS cache, SUM(estimated_cost_usd) AS cost
            FROM session_model_usage
            WHERE {OPENROUTER_SQL} AND first_seen >= strftime('%s','now',?)
            GROUP BY model ORDER BY cost DESC, inp DESC""",
        (f"-{days} days",),
    )
    out = []
    for r in rows:
        tokens = int(r["inp"] or 0) + int(r["out"] or 0) + int(r["cache"] or 0)
        cost = money_model_float(r["cost"])
        if tokens == 0 and cost == 0:
            continue
        out.append({
            "model": r["model"],
            "tokens": tokens,
            "input": int(r["inp"] or 0),
            "output": int(r["out"] or 0),
            "cache": int(r["cache"] or 0),
            "cost": cost,
        })
    return out


def recent_sessions(limit: int = 6) -> list:
    rows = db_rows(
        f"""SELECT id, title, model, started_at, estimated_cost_usd AS cost
            FROM sessions
            WHERE billing_provider='openrouter' OR billing_base_url LIKE '%openrouter.ai%'
            ORDER BY started_at DESC LIMIT {int(limit)}"""
    )
    out = []
    for r in rows:
        started = r["started_at"]
        out.append({
            "id": r["id"],
            "title": r["title"] or r["id"],
            "model": r["model"] or "",
            "started": time.strftime("%H:%M", time.localtime(started)) if started else "",
            "cost": money_model_float(r["cost"]),
        })
    return out


def agent_record(models_rows, days, today, all_time, account, key_present) -> dict:
    usage_by_model = {}
    for r in models_rows:
        usage_by_model[r["model"]] = {
            "inputTokens": r["input"],
            "outputTokens": r["output"],
            "cacheReadInputTokens": r["cache"],
            "cacheCreationInputTokens": 0,
        }

    today_by_model = {}
    for r in db_rows(
        f"""SELECT model AS model, SUM(input_tokens) AS inp, SUM(output_tokens) AS out,
                   SUM(cache_read_tokens) AS cache
            FROM session_model_usage
            WHERE {OPENROUTER_SQL} AND date(first_seen,'unixepoch','localtime') = date('now','localtime')
            GROUP BY model"""
    ):
        today_by_model[r["model"]] = int(r["inp"] or 0) + int(r["out"] or 0) + int(r["cache"] or 0)

    active_rows = db_rows(
        f"""SELECT DISTINCT date(first_seen,'unixepoch','localtime') AS d
            FROM session_model_usage
            WHERE {OPENROUTER_SQL} AND first_seen >= strftime('%s','now','-30 days')"""
    )
    active_dates = sorted(r["d"] for r in active_rows)

    today_sessions = db_rows(
        f"""SELECT COUNT(DISTINCT session_id) AS n FROM session_model_usage
            WHERE {OPENROUTER_SQL} AND date(first_seen,'unixepoch','localtime') = date('now','localtime')"""
    )
    total_sessions = db_rows(
        f"SELECT COUNT(DISTINCT session_id) AS n FROM session_model_usage WHERE {OPENROUTER_SQL}"
    )

    auth, status = "", ""
    if not key_present:
        auth = "Set OPENROUTER_API_KEY in ~/.hermes/.env"
        status = "Waiting for API key"
    elif not account:
        auth = "OpenRouter API unreachable — check network"
        status = "OpenRouter unavailable"

    return {
        "schemaVersion": 1,
        "id": "hermes",
        "name": "Hermes \u00b7 OpenRouter",
        "updatedAt": iso_now(),
        "ready": account is not None,
        "hasLocalStats": True,
        "scope": "account",
        "tierLabel": "OpenRouter",
        "usageStatusText": status,
        "authHelpText": auth,
        "todayPrompts": today["calls"],
        "todaySessions": today_sessions[0]["n"] if today_sessions else 0,
        "todayTotalTokens": today["tokens"],
        "todayTokensByModel": today_by_model,
        "recentDays": [{"date": d["date"], "messageCount": d["tokens"]} for d in days],
        "totalPrompts": 0,  # all-time prompts are not tracked cheaply
        "totalSessions": total_sessions[0]["n"] if total_sessions else 0,
        "activeDays": len(active_dates),
        "activeDates": active_dates,
        "modelUsage": usage_by_model,
        "limits": [],
        "balance": None
        if not account
        else {
            "remaining": round(account[2], 2),
            "funded": round(account[0], 2),
            "spent": round(account[1], 2),
            "currency": "USD",
            "estimated": False,
        },
    }


def _atomic_dump(obj, dest):
    tmp = dest + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh)
    os.replace(tmp, dest)



# ---------------------------------------------------------------------- main

def main():
    key = api_key()
    account = fetch_account(key)

    catalogue = fetch_models()
    model_id, provider = current_model()
    usage = usage_summary()
    by_day = by_day_rows()
    by_model = by_model_rows(30)
    sessions = recent_sessions(6)

    stats = {
        "schemaVersion": 1,
        "updated": iso_now(),
        "api": {
            "configured": bool(key),
            "ok": account is not None,
            "total": round(account[0], 2) if account else None,
            "used": round(account[1], 2) if account else None,
            "remaining": round(account[2], 2) if account else None,
        },
        "hermes": {
            "home": HERMES_HOME,
            "db": DB,
            "config": CFG,
            "model": model_id,
            "provider": provider,
        },
        "usage": {
            "today": usage["today"],
            "week": usage["week"],
            "allTime": usage["allTime"],
            "byDay": by_day,
            "byModel": by_model,
            "recentSessions": sessions,
        },
        "models": curated_models(catalogue, model_id),
    }
    _atomic_dump(stats, OUT)

    record = agent_record(by_model, by_day, usage["today"], usage["allTime"], account, bool(key))
    _atomic_dump(record, os.path.join(AGENTS_USAGE, "hermes.json"))

    bal = f"${stats['api']['remaining']:.2f} left" if stats["api"]["ok"] else "balance unavailable"
    print(f"hermes.openrouter: model={model_id} today={usage['today']['tokens']}t/${usage['today']['cost']:.2f} · {bal}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # never fail the shell silently
        sys.stderr.write(f"hermes.openrouter collect failed: {exc}\n")
        sys.exit(1)