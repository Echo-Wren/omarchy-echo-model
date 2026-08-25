#!/usr/bin/env python3
"""Collect Hermes/OpenRouter usage data for the hermes.openrouter bar widget.

Writes:
  $XDG_STATE_HOME/hermes-openrouter/stats.json        -> what Widget.qml displays
  $XDG_STATE_HOME/omarchy/agents/usage/hermes.json    -> optional omarchy.agents tab

Reads:
  <Hermes root>/state.db + profiles/*/state.db             -- local usage/cost
  <Hermes root>/config.yaml                                -- default model
  Profile .env files / $OPENROUTER_API_KEY                 -- API keys
  https://openrouter.ai/api/v1/credits, /auth/key, /models -- billing/catalogue

Never prints the API key.
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")


def resolve_hermes_root(active_home: str) -> str:
    """Resolve a profile home back to the installation's Hermes root."""
    active_home = os.path.abspath(os.path.expanduser(active_home))
    parent = os.path.dirname(active_home)
    if os.path.basename(parent) == "profiles":
        return os.path.dirname(parent)
    return active_home


ACTIVE_HERMES_HOME = os.environ.get("HERMES_HOME", os.path.join(HOME, ".hermes"))
HERMES_ROOT = resolve_hermes_root(ACTIVE_HERMES_HOME)
DEFAULT_HERMES_HOME = HERMES_ROOT
DB = os.path.join(DEFAULT_HERMES_HOME, "state.db")
CFG = os.path.join(DEFAULT_HERMES_HOME, "config.yaml")
ENV = os.path.join(DEFAULT_HERMES_HOME, ".env")
STATE_ROOT = os.environ.get("XDG_STATE_HOME", os.path.join(HOME, ".local", "state"))
OUT_DIR = os.path.join(STATE_ROOT, "hermes-openrouter")
OUT = os.path.join(OUT_DIR, "stats.json")
MODELS_CACHE = os.path.join(OUT_DIR, "models.json")
AGENTS_USAGE = os.path.join(STATE_ROOT, "omarchy", "agents", "usage")

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


def discover_profile_homes(root: str) -> list:
    """Default profile followed by named profile homes in stable order."""
    root = os.path.abspath(os.path.expanduser(root))
    homes = [{"name": "default", "home": root, "db": os.path.join(root, "state.db")}]
    profiles_dir = os.path.join(root, "profiles")
    try:
        names = sorted(entry.name for entry in os.scandir(profiles_dir) if entry.is_dir())
    except OSError:
        names = []
    for name in names:
        home = os.path.join(profiles_dir, name)
        homes.append({"name": name, "home": home, "db": os.path.join(home, "state.db")})
    return homes


def discover_profile_databases(root: str) -> list:
    """Readable profile DB candidates, with aliased real paths deduplicated."""
    rows, seen = [], set()
    for profile in discover_profile_homes(root):
        db_path = profile["db"]
        if not os.path.isfile(db_path):
            continue
        real = os.path.realpath(db_path)
        if real in seen:
            continue
        seen.add(real)
        rows.append(profile)
    return rows


def _env_api_key(path: str) -> str:
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("OPENROUTER_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("'\"").strip()
    except OSError:
        pass
    return ""


def openrouter_keys(profile_homes: list, environ=None) -> list:
    """Unique OpenRouter keys: process env, default, then named profiles."""
    environ = os.environ if environ is None else environ
    candidates = [str(environ.get("OPENROUTER_API_KEY", "")).strip()]
    candidates.extend(_env_api_key(os.path.join(p["home"], ".env")) for p in profile_homes)
    keys, seen = [], set()
    for key in candidates:
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse to follow any HTTP redirect.

    urllib copies the original request's headers (including Authorization)
    onto redirected requests regardless of the destination host, so following
    a redirect from the OpenRouter API could disclose the API key to a
    different host. The OpenRouter endpoints this plugin calls are direct
    JSON endpoints that never redirect; any 3xx is treated as a failure.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


_OPENER = urllib.request.build_opener(_RejectRedirects)


def fetch_json(url: str, headers: dict, timeout: int = 20):
    req = urllib.request.Request(url, headers=headers)
    with _OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def db_rows(db_path: str, sql: str, params=()):
    if not os.path.isfile(db_path):
        return []
    con = None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
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


def fetch_key_usage(key: str):
    """Actual billed usage for this API key, from /auth/key.

    OpenRouter's activity monitor shows the account-wide total; the key
    endpoint splits it per key so the widget can distinguish what Hermes
    itself actually cost from whatever else the account was used for.
    Returns {"total":..., "daily":..., "weekly":..., "monthly":...} or None.
    """
    if not key:
        return None
    try:
        data = fetch_json(f"{API_BASE}/auth/key", {"Authorization": f"Bearer {key}"})
        info = data.get("data") or {}
        return {
            "total": round(float(info.get("usage") or 0), 4),
            "daily": round(float(info.get("usage_daily") or 0), 4),
            "weekly": round(float(info.get("usage_weekly") or 0), 4),
            "monthly": round(float(info.get("usage_monthly") or 0), 4),
        }
    except Exception:
        return None


def aggregate_key_usage(keys: list):
    """Sum billed usage across unique keys, or None if any fetch fails."""
    unique = list(dict.fromkeys(key for key in keys if key))
    if not unique:
        return None
    totals = {"total": 0.0, "daily": 0.0, "weekly": 0.0, "monthly": 0.0}
    for key in unique:
        usage = fetch_key_usage(key)
        if usage is None:
            return None
        for field in totals:
            totals[field] += float(usage.get(field) or 0)
    return {field: round(value, 4) for field, value in totals.items()}


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
    # Defence in depth: only well-formed ids ever leave here. The widget runs
    # `hermes -p default config set model.default <id>`, so anything beyond a safe
    # charset (newlines, quotes, spaces, ...) is dropped before it can appear
    # in a command or a config file.
    safe_id = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,120}$")
    chosen, seen = [], set()

    def add(mid):
        if mid in seen or mid not in catalogue:
            return
        if not safe_id.match(mid):
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
        with open(CFG) as fh:
            lines = fh.read().splitlines()
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


def by_day_rows(profile_dbs: list) -> list:
    sql = f"""SELECT strftime('%Y-%m-%d', datetime(first_seen,'unixepoch','localtime')) AS d,
                     SUM(input_tokens) AS inp, SUM(output_tokens) AS out,
                     SUM(cache_read_tokens) AS cache, SUM(estimated_cost_usd) AS cost
              FROM session_model_usage
              WHERE {OPENROUTER_SQL} AND first_seen >= strftime('%s','now','-6 days')
              GROUP BY d"""
    by = {}
    for profile in profile_dbs:
        for row in db_rows(profile["db"], sql):
            day = row.get("d")
            if not day:
                continue
            merged = by.setdefault(day, {"inp": 0, "out": 0, "cache": 0, "cost": 0.0})
            merged["inp"] += int0(row.get("inp"))
            merged["out"] += int0(row.get("out"))
            merged["cache"] += int0(row.get("cache"))
            merged["cost"] += float(row.get("cost") or 0)

    out = []
    now = time.time()
    for i in range(6, -1, -1):
        day = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        r = by.get(day)
        tokens = r["inp"] + r["out"] + r["cache"] if r else 0
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


def usage_summary(profile_dbs: list) -> dict:
    base = (
        "SELECT SUM(input_tokens) AS inp, SUM(output_tokens) AS out, "
        "SUM(cache_read_tokens) AS cache, SUM(estimated_cost_usd) AS cost, "
        "SUM(CASE WHEN task='' THEN api_call_count ELSE 0 END) AS calls "
        f"FROM session_model_usage WHERE {OPENROUTER_SQL}"
    )
    def collect(where: str):
        total = {"inp": 0, "out": 0, "cache": 0, "cost": 0.0, "calls": 0}
        for profile in profile_dbs:
            rows = db_rows(profile["db"], base + where)
            row = rows[0] if rows else {}
            total["inp"] += int0(row.get("inp"))
            total["out"] += int0(row.get("out"))
            total["cache"] += int0(row.get("cache"))
            total["cost"] += float(row.get("cost") or 0)
            total["calls"] += int0(row.get("calls"))
        return {
            "tokens": total["inp"] + total["out"] + total["cache"],
            "cost": money_model_float(total["cost"]),
            "calls": total["calls"],
        }

    return {
        "today": collect(" AND date(first_seen,'unixepoch','localtime') = date('now','localtime')"),
        "week": collect(" AND first_seen >= strftime('%s','now','-7 days')"),
        "allTime": collect(""),
    }


def by_model_rows(profile_dbs: list, days: int = 30) -> list:
    sql = f"""SELECT model AS model, SUM(input_tokens) AS inp, SUM(output_tokens) AS out,
                     SUM(cache_read_tokens) AS cache, SUM(estimated_cost_usd) AS cost
              FROM session_model_usage
              WHERE {OPENROUTER_SQL} AND first_seen >= strftime('%s','now',?)
              GROUP BY model"""
    merged = {}
    for profile in profile_dbs:
        for row in db_rows(profile["db"], sql, (f"-{days} days",)):
            model = row.get("model") or ""
            total = merged.setdefault(model, {"inp": 0, "out": 0, "cache": 0, "cost": 0.0})
            total["inp"] += int0(row.get("inp"))
            total["out"] += int0(row.get("out"))
            total["cache"] += int0(row.get("cache"))
            total["cost"] += float(row.get("cost") or 0)

    out = []
    for model, row in merged.items():
        tokens = row["inp"] + row["out"] + row["cache"]
        cost = money_model_float(row["cost"])
        if tokens == 0 and cost == 0:
            continue
        out.append({
            "model": model,
            "tokens": tokens,
            "input": row["inp"],
            "output": row["out"],
            "cache": row["cache"],
            "cost": cost,
        })
    out.sort(key=lambda row: (-row["cost"], -row["input"], row["model"]))
    return out


def recent_sessions(profile_dbs: list, limit: int = 6) -> list:
    sql = f"""SELECT id, title, model, started_at, estimated_cost_usd AS cost
              FROM sessions
              WHERE billing_provider='openrouter' OR billing_base_url LIKE '%openrouter.ai%'
              ORDER BY started_at DESC LIMIT {int(limit)}"""
    rows = []
    for profile in profile_dbs:
        for row in db_rows(profile["db"], sql):
            row["profile"] = profile["name"]
            rows.append(row)
    rows.sort(key=lambda row: (-int0(row.get("started_at")), row["profile"], str(row.get("id") or "")))

    out = []
    for r in rows[:limit]:
        started = r["started_at"]
        out.append({
            "id": r["id"],
            "profile": r["profile"],
            "title": r["title"] or r["id"],
            "model": r["model"] or "",
            "started": time.strftime("%H:%M", time.localtime(started)) if started else "",
            "cost": money_model_float(r["cost"]),
        })
    return out


def agent_record(profile_dbs, models_rows, days, today, all_time, account, key_present) -> dict:
    usage_by_model = {}
    for r in models_rows:
        usage_by_model[r["model"]] = {
            "inputTokens": r["input"],
            "outputTokens": r["output"],
            "cacheReadInputTokens": r["cache"],
            "cacheCreationInputTokens": 0,
        }

    today_by_model = {}
    model_sql = f"""SELECT model AS model, SUM(input_tokens) AS inp, SUM(output_tokens) AS out,
                            SUM(cache_read_tokens) AS cache
                     FROM session_model_usage
                     WHERE {OPENROUTER_SQL}
                       AND date(first_seen,'unixepoch','localtime') = date('now','localtime')
                     GROUP BY model"""
    active_sql = f"""SELECT DISTINCT date(first_seen,'unixepoch','localtime') AS d
                      FROM session_model_usage
                      WHERE {OPENROUTER_SQL} AND first_seen >= strftime('%s','now','-30 days')"""
    today_sessions_sql = f"""SELECT COUNT(DISTINCT session_id) AS n FROM session_model_usage
                              WHERE {OPENROUTER_SQL}
                                AND date(first_seen,'unixepoch','localtime') = date('now','localtime')"""
    total_sessions_sql = (
        f"SELECT COUNT(DISTINCT session_id) AS n FROM session_model_usage WHERE {OPENROUTER_SQL}"
    )
    active_dates = set()
    today_sessions = 0
    total_sessions = 0
    for profile in profile_dbs:
        for row in db_rows(profile["db"], model_sql):
            tokens = int0(row.get("inp")) + int0(row.get("out")) + int0(row.get("cache"))
            today_by_model[row["model"]] = today_by_model.get(row["model"], 0) + tokens
        active_dates.update(
            row["d"] for row in db_rows(profile["db"], active_sql) if row.get("d")
        )
        rows = db_rows(profile["db"], today_sessions_sql)
        today_sessions += int0(rows[0].get("n")) if rows else 0
        rows = db_rows(profile["db"], total_sessions_sql)
        total_sessions += int0(rows[0].get("n")) if rows else 0
    active_dates = sorted(active_dates)

    auth, status = "", ""
    if not key_present:
        auth = "Set OPENROUTER_API_KEY in a Hermes profile .env"
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
        "todaySessions": today_sessions,
        "todayTotalTokens": today["tokens"],
        "todayTokensByModel": today_by_model,
        "recentDays": [{"date": d["date"], "messageCount": d["tokens"]} for d in days],
        "totalPrompts": 0,  # all-time prompts are not tracked cheaply
        "totalSessions": total_sessions,
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
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(AGENTS_USAGE, exist_ok=True)

    profile_homes = discover_profile_homes(HERMES_ROOT)
    profile_dbs = discover_profile_databases(HERMES_ROOT)
    keys = openrouter_keys(profile_homes)
    primary_key = keys[0] if keys else ""
    account = fetch_account(primary_key)
    key_usage = aggregate_key_usage(keys)

    catalogue = fetch_models()
    model_id, provider = current_model()
    usage = usage_summary(profile_dbs)
    by_day = by_day_rows(profile_dbs)
    by_model = by_model_rows(profile_dbs, 30)
    sessions = recent_sessions(profile_dbs, 6)
    profile_names = [profile["name"] for profile in profile_dbs]

    stats = {
        "schemaVersion": 1,
        "updated": iso_now(),
        "api": {
            "configured": bool(keys),
            "ok": account is not None,
            "total": round(account[0], 2) if account else None,
            "used": round(account[1], 2) if account else None,
            "remaining": round(account[2], 2) if account else None,
            "keyUsage": key_usage,
            "keyCount": len(keys),
            "keyUsageComplete": bool(keys) and key_usage is not None,
        },
        "hermes": {
            "home": DEFAULT_HERMES_HOME,
            "db": DB,
            "config": CFG,
            "model": model_id,
            "provider": provider,
            "profileCount": len(profile_names),
            "profiles": profile_names,
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

    record = agent_record(
        profile_dbs,
        by_model,
        by_day,
        usage["today"],
        usage["allTime"],
        account,
        bool(keys),
    )
    _atomic_dump(record, os.path.join(AGENTS_USAGE, "hermes.json"))

    bal = f"${stats['api']['remaining']:.2f} left" if stats["api"]["ok"] else "balance unavailable"
    print(
        f"hermes.openrouter: profiles={len(profile_names)} model={model_id} "
        f"today={usage['today']['tokens']}t/${usage['today']['cost']:.2f} · {bal}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # never fail the shell silently
        sys.stderr.write(f"hermes.openrouter collect failed: {exc}\n")
        sys.exit(1)