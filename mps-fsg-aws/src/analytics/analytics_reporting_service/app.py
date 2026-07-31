import json
import uuid
from datetime import datetime, timezone, date
from app_shared import (
    Router, json_response, parse_body, parse_path_params, parse_query_params,
    get_db_connection, set_rls_context, AppError, NotFoundError, ValidationError,
    make_handler, get_logger
)

router = Router()
logger = get_logger(__name__)

# ===== AnalyticsReportingService =====
# Completion metrics, report generation (Excel/CSV/scope export), Power BI Direct Query,
# metrics cache management.


def _get_completion_data(site_ids: list) -> list:
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute(
            """SELECT site_id,
                      COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE status = 'completed') AS completed,
                      COUNT(*) FILTER (WHERE status = 'canceled') AS canceled,
                      COUNT(*) FILTER (WHERE status = 'pending') AS pending
               FROM scheduled_occurrences
               WHERE site_id = ANY(%s)
               GROUP BY site_id""",
            (site_ids,)
        )
        rows = cur.fetchall()
    results = []
    for row in rows:
        total = row[1] or 0
        completed = row[2] or 0
        canceled = row[3] or 0
        pending = row[4] or 0
        overdue = 0
        rate = round((completed / total * 100), 2) if total > 0 else 0
        results.append({
            "site_id": str(row[0]), "total": total, "completed": completed,
            "canceled": canceled, "pending": pending, "overdue": overdue,
            "completionRate": rate, "byShift": [], "byCategory": [],
        })
    return results


# GET /reports/completion
@router.route("GET", "/reports/completion")
def completion_report(event, context, site_ids):
    data = _get_completion_data(site_ids)
    return json_response(data)


# GET /reports/audit
@router.route("GET", "/reports/audit")
def audit_report(event, context, site_ids):
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute(
            """SELECT cr.id, cr.scheduled_occurrence_id, so.scheduled_date, cr.reason_code, cr.notes, cr.canceled_at,
                      u.full_name AS canceled_by
               FROM cancellation_records cr
               JOIN scheduled_occurrences so ON cr.scheduled_occurrence_id = so.id
               JOIN users u ON cr.canceled_by_user_id = u.id
               WHERE so.site_id = ANY(%s)
               ORDER BY cr.canceled_at DESC
               LIMIT 500""",
            (site_ids,)
        )
        rows = cur.fetchall()
    return json_response([{
        "id": str(r[0]), "occurrence_id": str(r[1]), "scheduled_date": r[2].isoformat() if r[2] else None,
        "reason_code": r[3], "notes": r[4], "canceled_at": r[5].isoformat() if r[5] else None,
        "canceled_by": r[6],
    } for r in rows])


# GET /reports/trend
@router.route("GET", "/reports/trend")
def trend_report(event, context, site_ids):
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute(
            """SELECT
                 DATE_TRUNC('week', scheduled_date) AS period,
                 COUNT(*) AS total,
                 COUNT(*) FILTER (WHERE status = 'completed') AS completed
               FROM scheduled_occurrences
               WHERE site_id = ANY(%s)
               GROUP BY period
               ORDER BY period
               LIMIT 52""",
            (site_ids,)
        )
        rows = cur.fetchall()
    return json_response([{
        "period": r[0].isoformat() if r[0] else None, "total": r[1],
        "completed": r[2], "completionRate": round((r[2] / r[1] * 100), 2) if r[1] > 0 else 0,
    } for r in rows])


# GET /reports/drill
@router.route("GET", "/reports/drill")
def drill_report(event, context, site_ids):
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute(
            """SELECT so.site_id, sa.name AS area_name, st.service_type_name, so.status, COUNT(*) AS count
               FROM scheduled_occurrences so
               JOIN service_areas sa ON so.service_area_id = sa.id
               JOIN scope_tasks st ON so.scope_task_id = st.id
               WHERE so.site_id = ANY(%s)
               GROUP BY so.site_id, sa.name, st.service_type_name, so.status
               ORDER BY so.site_id, sa.name, st.service_type_name""",
            (site_ids,)
        )
        rows = cur.fetchall()
    tree = {}
    for r in rows:
        site_id = str(r[0])
        if site_id not in tree:
            tree[site_id] = {"site_id": site_id, "areas": {}}
        area = r[1]
        if area not in tree[site_id]["areas"]:
            tree[site_id]["areas"][area] = {"name": area, "categories": {}}
        cat = r[2]
        if cat not in tree[site_id]["areas"][area]["categories"]:
            tree[site_id]["areas"][area]["categories"][cat] = {"name": cat, "completed": 0, "canceled": 0, "pending": 0}
        tree[site_id]["areas"][area]["categories"][cat][r[3]] = r[4]
    return json_response(list(tree.values()))


# GET /reports/manhours
@router.route("GET", "/reports/manhours")
def manhours_report(event, context, site_ids):
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute(
            """SELECT sa.name AS area_name,
                      SUM(st.time_per_task_hours) AS budgeted_hours,
                      SUM(st.time_per_task_hours) FILTER (WHERE so.status = 'canceled') AS canceled_hours
               FROM scheduled_occurrences so
               JOIN scope_tasks st ON so.scope_task_id = st.id
               JOIN service_areas sa ON so.service_area_id = sa.id
               WHERE so.site_id = ANY(%s)
               GROUP BY sa.name
               ORDER BY sa.name""",
            (site_ids,)
        )
        rows = cur.fetchall()
    return json_response([{
        "area": r[0], "budgeted_hours": float(r[1]) if r[1] else 0,
        "canceled_hours": float(r[2]) if r[2] else 0,
    } for r in rows])


# GET /reports/powerbi
@router.route("GET", "/reports/powerbi")
def powerbi_export(event, context, site_ids):
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute(
            "SELECT id, site_id, service_area_id, scheduled_date, status FROM scheduled_occurrences WHERE site_id = ANY(%s) LIMIT 1000",
            (site_ids,)
        )
        rows = cur.fetchall()
    facts = [{"occurrence_id": str(r[0]), "site_id": str(r[1]), "area_id": str(r[2]), "date": r[3].isoformat() if r[3] else None, "status": r[4]} for r in rows]
    return json_response({"grain": "occurrence", "facts": facts, "dimensions": ["site", "area", "date", "status"], "refresh": datetime.now(timezone.utc).isoformat()})


# ===== TASK SCHEDULES ANALYTICS =====

# GET /task-schedules:burndown
@router.route("GET", "/task-schedules:burndown")
def burndown(event, context, site_ids):
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute(
            """SELECT
                 scheduled_date,
                 COUNT(*) AS expected,
                 COUNT(*) FILTER (WHERE status = 'completed') AS actual
               FROM scheduled_occurrences
               WHERE site_id = ANY(%s)
               GROUP BY scheduled_date
               ORDER BY scheduled_date""",
            (site_ids,)
        )
        rows = cur.fetchall()
    baseline = sum(r[1] for r in rows)
    actual = sum(r[2] for r in rows)
    series = [{"date": r[0].isoformat() if r[0] else None, "expected": r[1], "actual": r[2]} for r in rows]
    return json_response({
        "baseline": baseline, "actual": actual, "projectedEoy": actual,
        "expected": baseline, "behindAhead": actual - baseline,
        "series": series, "drill": [],
    })


# GET /task-schedules:density
@router.route("GET", "/task-schedules:density")
def density(event, context, site_ids):
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute(
            "SELECT scheduled_date, COUNT(*) AS count FROM scheduled_occurrences WHERE site_id = ANY(%s) AND scheduled_date >= CURRENT_DATE - INTERVAL '90 days' GROUP BY scheduled_date ORDER BY scheduled_date",
            (site_ids,)
        )
        rows = cur.fetchall()
    return json_response([{"date": r[0].isoformat() if r[0] else None, "count": r[1]} for r in rows])


# ===== REPORT TEMPLATES (in-memory store — Phase 6 replace with DB) =====
_templates_store = {}
_schedules_store = {}
_threshold_rules_store = {}
_deliveries_store = []


# Templates
@router.route("GET", "/reports/templates")
def list_templates(event, context, site_ids):
    return json_response({"data": list(_templates_store.values()), "meta": {"total": len(_templates_store)}})


@router.route("POST", "/reports/templates")
def create_template(event, context, site_ids):
    body = parse_body(event)
    tid = str(uuid.uuid4())
    tmpl = {"id": tid, "name": body.get("name", "Untitled"), "report_type": body.get("report_type", "completion"), "config": body.get("config", {}), "created_at": datetime.now(timezone.utc).isoformat()}
    _templates_store[tid] = tmpl
    return json_response(tmpl, 201)


@router.route("GET", "/reports/templates/{templateId}")
def get_template(event, context, site_ids):
    tid = parse_path_params(event).get("templateId")
    if tid not in _templates_store:
        raise NotFoundError("Template not found")
    return json_response(_templates_store[tid])


@router.route("PUT", "/reports/templates/{templateId}")
def update_template(event, context, site_ids):
    tid = parse_path_params(event).get("templateId")
    if tid not in _templates_store:
        raise NotFoundError("Template not found")
    body = parse_body(event)
    _templates_store[tid].update({k: v for k, v in body.items() if k in ("name", "report_type", "config")})
    _templates_store[tid]["updated_at"] = datetime.now(timezone.utc).isoformat()
    return json_response(_templates_store[tid])


@router.route("DELETE", "/reports/templates/{templateId}")
def delete_template(event, context, site_ids):
    tid = parse_path_params(event).get("templateId")
    _templates_store.pop(tid, None)
    return json_response(None, 204)


# Schedules
@router.route("GET", "/reports/schedules")
def list_schedules(event, context, site_ids):
    return json_response({"data": list(_schedules_store.values()), "meta": {"total": len(_schedules_store)}})


@router.route("POST", "/reports/schedules")
def create_schedule(event, context, site_ids):
    body = parse_body(event)
    sid = str(uuid.uuid4())
    sched = {"id": sid, "template_id": body.get("template_id"), "cron": body.get("cron", "0 8 * * MON"), "active": True, "created_at": datetime.now(timezone.utc).isoformat()}
    _schedules_store[sid] = sched
    return json_response(sched, 201)


@router.route("GET", "/reports/schedules/{scheduleId}")
def get_schedule(event, context, site_ids):
    sid = parse_path_params(event).get("scheduleId")
    if sid not in _schedules_store:
        raise NotFoundError("Schedule not found")
    return json_response(_schedules_store[sid])


@router.route("PUT", "/reports/schedules/{scheduleId}")
def update_schedule(event, context, site_ids):
    sid = parse_path_params(event).get("scheduleId")
    if sid not in _schedules_store:
        raise NotFoundError("Schedule not found")
    body = parse_body(event)
    _schedules_store[sid].update({k: v for k, v in body.items() if k in ("template_id", "cron", "active")})
    return json_response(_schedules_store[sid])


@router.route("DELETE", "/reports/schedules/{scheduleId}")
def delete_schedule(event, context, site_ids):
    sid = parse_path_params(event).get("scheduleId")
    _schedules_store.pop(sid, None)
    return json_response(None, 204)


# Threshold Rules
@router.route("GET", "/reports/threshold-rules")
def list_threshold_rules(event, context, site_ids):
    return json_response({"data": list(_threshold_rules_store.values()), "meta": {"total": len(_threshold_rules_store)}})


@router.route("POST", "/reports/threshold-rules")
def create_threshold_rule(event, context, site_ids):
    body = parse_body(event)
    rid = str(uuid.uuid4())
    rule = {"id": rid, "site_id": body.get("site_id"), "metric": body.get("metric", "completion_rate"), "operator": body.get("operator", "less_than"), "value": body.get("value", 90), "active": True}
    _threshold_rules_store[rid] = rule
    return json_response(rule, 201)


@router.route("GET", "/reports/threshold-rules/{ruleId}")
def get_threshold_rule(event, context, site_ids):
    rid = parse_path_params(event).get("ruleId")
    if rid not in _threshold_rules_store:
        raise NotFoundError("Threshold rule not found")
    return json_response(_threshold_rules_store[rid])


@router.route("PUT", "/reports/threshold-rules/{ruleId}")
def update_threshold_rule(event, context, site_ids):
    rid = parse_path_params(event).get("ruleId")
    if rid not in _threshold_rules_store:
        raise NotFoundError("Threshold rule not found")
    body = parse_body(event)
    _threshold_rules_store[rid].update({k: v for k, v in body.items() if k in ("metric", "operator", "value", "active")})
    return json_response(_threshold_rules_store[rid])


@router.route("DELETE", "/reports/threshold-rules/{ruleId}")
def delete_threshold_rule(event, context, site_ids):
    rid = parse_path_params(event).get("ruleId")
    _threshold_rules_store.pop(rid, None)
    return json_response(None, 204)


# Deliveries
@router.route("GET", "/reports/deliveries")
def list_deliveries(event, context, site_ids):
    return json_response({"data": _deliveries_store, "meta": {"total": len(_deliveries_store)}})


@router.route("POST", "/reports/deliveries")
def send_delivery(event, context, site_ids):
    body = parse_body(event)
    delivery = {"id": str(uuid.uuid4()), "template_id": body.get("template_id"), "sent_at": datetime.now(timezone.utc).isoformat(), "status": "sent"}
    _deliveries_store.append(delivery)
    return json_response(delivery, 201)


lambda_handler = make_handler(router)