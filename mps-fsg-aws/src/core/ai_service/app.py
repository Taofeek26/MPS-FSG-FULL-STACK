import json
import os
from datetime import datetime, timezone
from app_shared import (
    Router, json_response, parse_body, get_secret,
    get_db_connection, set_rls_context, AppError, NotFoundError, ValidationError,
    make_handler, get_logger
)

router = Router()
logger = get_logger(__name__)

# ===== AIService =====
# AI-powered scheduling suggestions and natural language query execution.
# Calls external AI API via NAT Gateway. Conditional: only deployed when EnableAI=true.


def _call_ai_api(prompt: str, system_prompt: str = "") -> str:
    """Call external AI API via HTTPS."""
    import urllib.request, urllib.error
    secret = get_secret(os.environ["AI_SECRET_NAME"])
    api_key = secret.get("api_key", "")

    url = "https://api.openai.com/v1/chat/completions"
    body = json.dumps({
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": int(os.environ.get("AI_MAX_TOKENS", 2048)),
        "temperature": 0.3,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")

    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"]
    except urllib.error.URLError as e:
        logger.exception(f"AI API call failed: {e}")
        raise AppError(f"AI API call failed: {str(e)}", 502)


@router.route("POST", "/ai/scheduling-suggestions")
def scheduling_suggestions(event, context, site_ids):
    body = parse_body(event)
    site_id = body.get("site_id", site_ids[0] if site_ids else "")

    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute(
            "SELECT service_type_name, COUNT(*) AS pending_count FROM scheduled_occurrences so JOIN scope_tasks st ON so.scope_task_id = st.id WHERE so.site_id = %s AND so.status = 'pending' GROUP BY service_type_name ORDER BY pending_count DESC LIMIT 10",
            (site_id,)
        )
        pending = [{"task": r[0], "count": r[1]} for r in cur.fetchall()]
        cur.execute(
            "SELECT service_type_name, COUNT(*) AS overdue FROM scheduled_occurrences so JOIN scope_tasks st ON so.scope_task_id = st.id WHERE so.site_id = %s AND so.status != 'completed' AND so.scheduled_date < CURRENT_DATE GROUP BY service_type_name ORDER BY overdue DESC LIMIT 10",
            (site_id,)
        )
        overdue = [{"task": r[0], "count": r[1]} for r in cur.fetchall()]

    prompt = json.dumps({"pending_tasks": pending, "overdue_tasks": overdue, "context": body.get("context", {})})
    system = "You are a facilities services scheduling assistant. Given pending and overdue task data, suggest the top 3-5 optimization actions like rebalancing shift loads, rescheduling overdue items, or flagging areas needing attention. Return valid JSON with keys: recommendations (list of objects with action, priority, reason)."

    response = _call_ai_api(prompt, system)
    try:
        result = json.loads(response)
    except json.JSONDecodeError:
        result = {"raw_response": response}
    return json_response(result)


@router.route("POST", "/ai/nl-query")
@router.route("POST", "/ai/recommendations")
def nl_query(event, context, site_ids):
    body = parse_body(event)
    question = body.get("question", body.get("query", ""))

    if not question:
        raise ValidationError("question is required")

    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute(
            """SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' AND table_name NOT LIKE 'audit_log_%%'"""
        )
        tables = [r[0] for r in cur.fetchall()]

    schema_context = {"available_tables": tables, "question": question}
    system = f"You are a PostgreSQL query generator for a facilities services application. Available tables: {', '.join(tables)}. Given a plain-English question, return valid JSON with keys: sql (a parameterized SQL query safe against injection) and explanation (what the query does). Use $1, $2 for parameters."

    response = _call_ai_api(json.dumps(schema_context), system)
    try:
        result = json.loads(response)
    except json.JSONDecodeError:
        result = {"raw_response": response}

    result["original_question"] = question
    return json_response(result)


# GET /ai/recommendations
@router.route("GET", "/ai/recommendations")
def list_recommendations(event, context, site_ids):
    return json_response({"data": []})


lambda_handler = make_handler(router)