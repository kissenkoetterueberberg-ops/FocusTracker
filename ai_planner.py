"""AI Day Planner — generates structured time-blocked day plans.

Provider order (automatic):
  1. Groq (Llama 3.3 70B) — free tier, default when GROQ_API_KEY is set
  2. Anthropic (Claude Sonnet) — fallback

Configure via env vars or the `config` SQLite table:
  GROQ_API_KEY      / config.groq_api_key
  ANTHROPIC_API_KEY / config.anthropic_api_key
  FOCUSTRACKER_AI_PROVIDER / config.ai_provider  ("groq" | "anthropic" | "auto")
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
ANTHROPIC_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are an expert productivity coach specializing in deep work, time-blocking, and Eisenhower prioritization. Your job is to create realistic, actionable daily schedules.

Core principles you apply:
- Deep Work first: Schedule cognitively demanding tasks 9:00–12:00 (peak brain performance)
- Admin/Email/Calls: 13:00–15:00 (post-lunch, lower cognitive load)
- Quick wins / light tasks: 15:00–17:00
- Always add 15–20% buffer time (things always take longer than expected)
- Group similar tasks to minimize context switching
- Eisenhower quadrant: Urgent+Important → morning, Important+Not Urgent → early afternoon, Urgent+Not Important → delegate or late afternoon
- Be realistic: a typical human can do max 4–5 hours of real deep work per day

You must return ONLY valid JSON — no markdown, no explanation, no code fences. Just the raw JSON object."""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "blocks": {
            "type": "array",
            "description": "Time blocks in chronological order",
            "items": {
                "type": "object",
                "properties": {
                    "start_time": {"type": "string", "description": "Start time HH:MM (24h)"},
                    "duration_min": {"type": "integer", "description": "Duration in minutes (15–180)"},
                    "title": {"type": "string", "description": "Action-oriented task title (max 60 chars)"},
                    "description": {"type": "string", "description": "Optional brief note (max 120 chars)"},
                    "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                    "category": {
                        "type": "string",
                        "enum": ["deep_work", "admin", "communication", "meeting", "break", "learning", "other"],
                    },
                    "is_carry_over": {"type": "boolean"},
                },
                "required": ["start_time", "duration_min", "title", "priority", "category"],
            },
        },
        "summary": {"type": "string", "description": "1–2 sentence coaching note"},
        "total_deep_work_min": {"type": "integer"},
        "total_admin_min": {"type": "integer"},
    },
    "required": ["blocks", "summary"],
}

ANTHROPIC_TOOL = {
    "name": "create_day_plan",
    "description": "Create a structured, time-blocked day plan from a list of tasks.",
    "input_schema": PLAN_SCHEMA,
}


# ---------- Config lookup ----------

def _config_get(db_path: Path, key: str) -> str | None:
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _resolve_key(env_name: str, config_key: str, db_path: Path) -> str | None:
    return os.environ.get(env_name) or _config_get(db_path, config_key)


def _resolve_provider(db_path: Path) -> tuple[str | None, str | None]:
    """Returns (provider, reason). Reason is a user-facing hint when provider is None."""
    explicit = os.environ.get("FOCUSTRACKER_AI_PROVIDER") or _config_get(db_path, "ai_provider")
    if explicit and explicit != "auto":
        return explicit.lower(), None

    groq = _resolve_key("GROQ_API_KEY", "groq_api_key", db_path)
    if groq:
        return "groq", None
    anth = _resolve_key("ANTHROPIC_API_KEY", "anthropic_api_key", db_path)
    if anth:
        return "anthropic", None
    return None, (
        "Kein AI-Key gesetzt. Trag einen kostenlosen Groq-Key in den Einstellungen ein "
        "(console.groq.com → API Keys) oder setze GROQ_API_KEY / ANTHROPIC_API_KEY als Env-Var."
    )


# ---------- Prompt building ----------

def _build_user_msg(raw_todos: str, context: dict) -> str:
    work_hours = context.get("work_hours_target", 6)
    date_str = context.get("date", datetime.now().strftime("%Y-%m-%d"))
    weekday = context.get("weekday", datetime.now().strftime("%A"))
    carried = context.get("carried_todos", [])

    carried_section = ""
    if carried:
        lines = "\n".join(
            f"- {t['title']} (from {t.get('carried_from_date', 'yesterday')})"
            for t in carried
        )
        carried_section = f"\n\n**Unfinished tasks from previous day (carry-overs):**\n{lines}"

    return (
        f"Today is {weekday}, {date_str}. The user wants to work ~{work_hours} hours.\n\n"
        f"**Their brain-dump of today's tasks:**\n{raw_todos.strip()}{carried_section}\n\n"
        "Create a realistic, time-blocked day plan. Start the first deep work block at 09:00 "
        "unless the user specifies otherwise. Include a lunch break if the plan spans past 13:00. "
        "Mark carried-over tasks with is_carry_over: true."
    )


def _normalize_plan(result: dict) -> dict:
    result.setdefault("blocks", [])
    result.setdefault("summary", "")
    result.setdefault("total_deep_work_min", 0)
    result.setdefault("total_admin_min", 0)
    return result


# ---------- Groq provider ----------

def _call_groq(api_key: str, user_msg: str) -> dict:
    if not _HTTPX_AVAILABLE:
        return {"error": "httpx not installed (pip install httpx)", "blocks": []}

    groq_system = (
        SYSTEM_PROMPT
        + "\n\nReturn a JSON object with this exact shape:\n"
        + json.dumps({
            "blocks": [{
                "start_time": "HH:MM",
                "duration_min": 60,
                "title": "...",
                "description": "...",
                "priority": "critical|high|medium|low",
                "category": "deep_work|admin|communication|meeting|break|learning|other",
                "is_carry_over": False,
            }],
            "summary": "...",
            "total_deep_work_min": 0,
            "total_admin_min": 0,
        }, indent=2)
    )

    payload = {
        "model": GROQ_MODEL,
        "temperature": 0.3,
        "max_tokens": 2048,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": groq_system},
            {"role": "user", "content": user_msg},
        ],
    }

    try:
        resp = httpx.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60.0,
        )
        if resp.status_code != 200:
            return {
                "error": f"Groq API {resp.status_code}: {resp.text[:200]}",
                "blocks": [],
            }
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        result = _parse_json_loose(content)
        if not isinstance(result, dict):
            return {"error": "Groq returned non-object JSON", "blocks": []}
        return _normalize_plan(result)
    except Exception as e:
        return {"error": f"Groq request failed: {e}", "blocks": []}


def _parse_json_loose(text: str) -> dict | None:
    """Parse JSON from a string, tolerating code fences or stray prose."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # strip code fences
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    # first top-level object
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return None


# ---------- Anthropic provider ----------

def _call_anthropic(api_key: str, user_msg: str) -> dict:
    if not _ANTHROPIC_AVAILABLE:
        return {"error": "anthropic package not installed (pip install anthropic)", "blocks": []}
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=[ANTHROPIC_TOOL],
            tool_choice={"type": "tool", "name": "create_day_plan"},
            messages=[{"role": "user", "content": user_msg}],
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == "create_day_plan":
                return _normalize_plan(dict(block.input))
        return {"error": "Claude returned no tool_use block", "blocks": []}
    except Exception as e:
        return {"error": f"Anthropic request failed: {e}", "blocks": []}


# ---------- Public API ----------

def generate_day_plan(raw_todos: str, context: dict, db_path: Path) -> dict:
    """
    Generate a structured day plan. Provider is chosen automatically:
    Groq (free) if GROQ_API_KEY is present, otherwise Anthropic.

    Returns:
        dict with keys: blocks, summary, total_deep_work_min, total_admin_min, provider
        On error: {"error": str, "blocks": []}
    """
    provider, reason = _resolve_provider(db_path)
    if provider is None:
        return {"error": reason, "blocks": []}

    user_msg = _build_user_msg(raw_todos, context)

    if provider == "groq":
        key = _resolve_key("GROQ_API_KEY", "groq_api_key", db_path)
        if not key:
            return {"error": "ai_provider=groq aber kein GROQ_API_KEY gesetzt.", "blocks": []}
        result = _call_groq(key, user_msg)
        if "error" not in result or result.get("blocks"):
            result["provider"] = "groq"
            return result
        # Auto-fallback to Anthropic if Groq fails and Anthropic key exists
        anth_key = _resolve_key("ANTHROPIC_API_KEY", "anthropic_api_key", db_path)
        if anth_key:
            fallback = _call_anthropic(anth_key, user_msg)
            if "error" not in fallback or fallback.get("blocks"):
                fallback["provider"] = "anthropic (fallback)"
                fallback["groq_error"] = result.get("error")
                return fallback
        result["provider"] = "groq"
        return result

    if provider == "anthropic":
        key = _resolve_key("ANTHROPIC_API_KEY", "anthropic_api_key", db_path)
        if not key:
            return {"error": "ai_provider=anthropic aber kein ANTHROPIC_API_KEY gesetzt.", "blocks": []}
        result = _call_anthropic(key, user_msg)
        result["provider"] = "anthropic"
        return result

    return {"error": f"Unknown provider '{provider}'", "blocks": []}
