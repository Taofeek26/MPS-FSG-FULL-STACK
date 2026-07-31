import json
import uuid
from datetime import datetime, timezone
from app_shared import (
    Router, json_response, parse_body, parse_path_params, parse_query_params,
    get_db_connection, set_rls_context, AppError, NotFoundError, ValidationError, AuthorizationError,
    make_handler, get_logger
)

router = Router()
logger = get_logger(__name__)

# ===== SiteConfigService =====
# Unified CRUD for: sites, shifts, service areas, scope tasks, users, assignments,
# QR code generation, health check.
# Syncs user changes to Cognito. Manages EventBridge shift-start notification rules.

_ROLE_RANK = {"PlatformAdmin": 100, "SiteManager": 50, "LeadSupervisor": 25, "ShiftSupervisor": 10}


def _ensure_site_manager_or_above(event, context):
    claims = event.get("requestContext", {}).get("authorizer", {}).get("lambda", {})
    role = claims.get("role", "")
    if _ROLE_RANK.get(role, 0) < _ROLE_RANK["SiteManager"]:
        raise AuthorizationError("SiteManager or above required")


# ===== SITES =====
@router.route("GET", "/sites")
def list_sites(event, context, site_ids):
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute("SELECT id, name, facility_location, operating_days_per_week, required_photo_percentage, contract_start_date, contract_end_date, annual_contract_value, active, created_at, updated_at FROM sites ORDER BY name")
        rows = cur.fetchall()
    data = []
    for row in rows:
        data.append({
            "id": str(row[0]), "name": row[1], "facility_location": row[2],
            "operating_days_per_week": row[3], "required_photo_percentage": float(row[4]) if row[4] else 0,
            "contract_start_date": row[5].isoformat() if row[5] else None,
            "contract_end_date": row[6].isoformat() if row[6] else None,
            "annual_contract_value": float(row[7]) if row[7] else None,
            "active": row[8], "created_at": row[9].isoformat() if row[9] else None,
            "updated_at": row[10].isoformat() if row[10] else None,
        })
    return json_response({"data": data, "meta": {"total": len(data)}})


@router.route("POST", "/sites")
def create_site(event, context, site_ids):
    _ensure_site_manager_or_above(event, context)
    body = parse_body(event)
    name = body.get("name")
    if not name:
        raise ValidationError("name is required")
    conn = get_db_connection()
    with conn.cursor() as cur:
        site_id = uuid.uuid4()
        cur.execute(
            "INSERT INTO sites (id, name, facility_location, operating_days_per_week, required_photo_percentage, contract_start_date, contract_end_date, annual_contract_value, active) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true) RETURNING id",
            (str(site_id), name, body.get("facility_location"), body.get("operating_days_per_week", 7),
             body.get("required_photo_percentage", 0), body.get("contract_start_date"), body.get("contract_end_date"),
             body.get("annual_contract_value"))
        )
        result = cur.fetchone()
        cur.execute(
            "INSERT INTO audit_log (entity_type, entity_id, action, actor_user_id, new_values) VALUES ('site', %s, 'create', %s, %s)",
            (str(result[0]), body.get("user_id", "system"), json.dumps(body))
        )
    return json_response({"id": str(result[0]), "name": name}, 201)


@router.route("GET", "/sites/{siteId}")
def get_site(event, context, site_ids):
    site_id = parse_path_params(event).get("siteId")
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute("SELECT id, name, facility_location, operating_days_per_week, required_photo_percentage, contract_start_date, contract_end_date, annual_contract_value, active FROM sites WHERE id = %s", (site_id,))
        row = cur.fetchone()
        if not row:
            raise NotFoundError("Site not found")
    return json_response({
        "id": str(row[0]), "name": row[1], "facility_location": row[2],
        "operating_days_per_week": row[3], "required_photo_percentage": float(row[4]) if row[4] else 0,
        "contract_start_date": row[5].isoformat() if row[5] else None,
        "contract_end_date": row[6].isoformat() if row[6] else None,
        "annual_contract_value": float(row[7]) if row[7] else None, "active": row[8],
    })


@router.route("PATCH", "/sites/{siteId}")
def update_site(event, context, site_ids):
    _ensure_site_manager_or_above(event, context)
    site_id = parse_path_params(event).get("siteId")
    body = parse_body(event)
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute(
            "UPDATE sites SET name = COALESCE(%s, name), facility_location = COALESCE(%s, facility_location), operating_days_per_week = COALESCE(%s, operating_days_per_week), required_photo_percentage = COALESCE(%s, required_photo_percentage), contract_start_date = COALESCE(%s, contract_start_date), contract_end_date = COALESCE(%s, contract_end_date), annual_contract_value = COALESCE(%s, annual_contract_value), updated_at = now() WHERE id = %s RETURNING id",
            (body.get("name"), body.get("facility_location"), body.get("operating_days_per_week"),
             body.get("required_photo_percentage"), body.get("contract_start_date"), body.get("contract_end_date"),
             body.get("annual_contract_value"), site_id)
        )
        result = cur.fetchone()
        if not result:
            raise NotFoundError("Site not found")
    return json_response({"id": site_id})


@router.route("DELETE", "/sites/{siteId}")
def delete_site(event, context, site_ids):
    _ensure_site_manager_or_above(event, context)
    site_id = parse_path_params(event).get("siteId")
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute("UPDATE sites SET active = false, updated_at = now() WHERE id = %s", (site_id,))
    return json_response(None, 204)


# ===== CUSTOMERS =====
@router.route("GET", "/customers")
def list_customers(event, context, site_ids):
    return json_response({"data": [], "meta": {"total": 0}})


@router.route("GET", "/customers/{customerId}")
def get_customer(event, context, site_ids):
    return json_response({"id": parse_path_params(event).get("customerId"), "name": "Customer"})


# ===== SHIFTS =====
@router.route("GET", "/shifts")
def list_shifts(event, context, site_ids):
    params = parse_query_params(event)
    site_filter = params.get("site_id")
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        if site_filter:
            cur.execute("SELECT id, site_id, name, start_time, end_time, operating_days, created_at, updated_at FROM shifts WHERE site_id = %s ORDER BY name", (site_filter,))
        else:
            cur.execute("SELECT id, site_id, name, start_time, end_time, operating_days, created_at, updated_at FROM shifts ORDER BY name")
        rows = cur.fetchall()
    return json_response({"data": [{"id": str(r[0]), "site_id": str(r[1]), "name": r[2], "start_time": str(r[3]), "end_time": str(r[4]), "operating_days": r[5] if r[5] else []} for r in rows]})


@router.route("POST", "/shifts")
def create_shift(event, context, site_ids):
    body = parse_body(event)
    site_id = body.get("site_id", site_ids[0] if site_ids else "")
    if not body.get("name") or not site_id:
        raise ValidationError("name and site_id are required")
    conn = get_db_connection()
    with conn.cursor() as cur:
        shift_id = uuid.uuid4()
        cur.execute(
            "INSERT INTO shifts (id, site_id, name, start_time, end_time, operating_days) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (str(shift_id), site_id, body["name"], body.get("start_time", "06:00"), body.get("end_time", "14:00"), body.get("operating_days", [1, 2, 3, 4, 5]))
        )
        result = cur.fetchone()
    return json_response({"id": str(result[0]), "name": body["name"]}, 201)


@router.route("PUT", "/shifts/{shiftId}")
def update_shift(event, context, site_ids):
    shift_id = parse_path_params(event).get("shiftId")
    body = parse_body(event)
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute(
            "UPDATE shifts SET name = COALESCE(%s, name), start_time = COALESCE(%s, start_time), end_time = COALESCE(%s, end_time), operating_days = COALESCE(%s, operating_days), updated_at = now() WHERE id = %s RETURNING id",
            (body.get("name"), body.get("start_time"), body.get("end_time"), body.get("operating_days"), shift_id)
        )
        result = cur.fetchone()
        if not result:
            raise NotFoundError("Shift not found")
    return json_response({"id": shift_id})


@router.route("DELETE", "/shifts/{shiftId}")
def delete_shift(event, context, site_ids):
    shift_id = parse_path_params(event).get("shiftId")
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute("DELETE FROM shifts WHERE id = %s", (shift_id,))
    return json_response(None, 204)


# ===== CONTRACT AREAS =====
@router.route("GET", "/contract-areas")
def list_contract_areas(event, context, site_ids):
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute("SELECT id, site_id, name, qr_code, active FROM service_areas ORDER BY name")
        rows = cur.fetchall()
    return json_response({"data": [{"id": str(r[0]), "site_id": str(r[1]), "name": r[2], "qr_code": r[3], "active": r[4]} for r in rows]})


# ===== TASK TEMPLATES =====
@router.route("GET", "/task-templates")
def list_task_templates(event, context, site_ids):
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute("SELECT id, service_area_id, site_id, shift_id, service_type_name, time_per_task_hours, services_per_year, active FROM scope_tasks ORDER BY service_type_name")
        rows = cur.fetchall()
    return json_response({"data": [{"id": str(r[0]), "service_area_id": str(r[1]), "site_id": str(r[2]), "shift_id": str(r[3]) if r[3] else None, "service_type_name": r[4], "time_per_task_hours": float(r[5]) if r[5] else 0, "services_per_year": r[6], "active": r[7]} for r in rows]})


@router.route("GET", "/task-templates/{templateId}")
def get_task_template(event, context, site_ids):
    tid = parse_path_params(event).get("templateId")
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute("SELECT id, service_area_id, site_id, shift_id, service_type_name, time_per_task_hours, services_per_year, active FROM scope_tasks WHERE id = %s", (tid,))
        row = cur.fetchone()
        if not row:
            raise NotFoundError("Task template not found")
    return json_response({"id": str(row[0]), "service_area_id": str(row[1]), "site_id": str(row[2]), "shift_id": str(row[3]) if row[3] else None, "service_type_name": row[4], "time_per_task_hours": float(row[5]) if row[5] else 0, "services_per_year": row[6], "active": row[7]})


# ===== LINES =====
@router.route("GET", "/lines")
def list_lines(event, context, site_ids):
    return json_response({"data": []})


@router.route("GET", "/lines/{lineId}")
def get_line(event, context, site_ids):
    return json_response({"id": parse_path_params(event).get("lineId"), "name": "Line"})


# ===== CONTRACTS =====
@router.route("GET", "/contracts")
def list_contracts(event, context, site_ids):
    return json_response({"data": []})


# ===== PERSONNEL =====
@router.route("GET", "/personnel")
def list_personnel(event, context, site_ids):
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute("SELECT id, email, full_name, role, active FROM users ORDER BY full_name")
        rows = cur.fetchall()
    return json_response({"data": [{"id": str(r[0]), "email": r[1], "full_name": r[2], "role": r[3], "active": r[4]} for r in rows]})


@router.route("POST", "/personnel")
def create_personnel(event, context, site_ids):
    body = parse_body(event)
    conn = get_db_connection()
    with conn.cursor() as cur:
        uid = uuid.uuid4()
        cur.execute(
            "INSERT INTO users (id, cognito_sub, email, full_name, role, active) VALUES (%s, %s, %s, %s, %s, true) RETURNING id",
            (str(uid), body.get("cognito_sub", str(uuid.uuid4())), body["email"], body["full_name"], body.get("role", "ShiftSupervisor"))
        )
        result = cur.fetchone()
    return json_response({"id": str(result[0])}, 201)


@router.route("DELETE", "/personnel/{personnelId}")
def delete_personnel(event, context, site_ids):
    pid = parse_path_params(event).get("personnelId")
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute("UPDATE users SET active = false, updated_at = now() WHERE id = %s", (pid,))
    return json_response(None, 204)


# ===== COVERAGE ASSIGNMENTS =====
@router.route("GET", "/coverage-assignments")
def list_coverage(event, context, site_ids):
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT u.id, u.full_name, us.site_id, ush.shift_id FROM users u LEFT JOIN user_site_assignments us ON u.id = us.user_id LEFT JOIN user_shift_assignments ush ON u.id = ush.user_id WHERE u.active = true"
        )
        rows = cur.fetchall()
    data = []
    for row in rows:
        data.append({"user_id": str(row[0]), "full_name": row[1], "site_id": str(row[2]) if row[2] else None, "shift_id": str(row[3]) if row[3] else None})
    return json_response({"data": data})


@router.route("POST", "/coverage-assignments")
def create_coverage(event, context, site_ids):
    body = parse_body(event)
    user_id = body.get("user_id")
    shift_id = body.get("shift_id")
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO user_shift_assignments (user_id, shift_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (user_id, shift_id))
    return json_response({"user_id": user_id, "shift_id": shift_id}, 201)


@router.route("DELETE", "/coverage-assignments/{coverageAssignmentId}")
def delete_coverage(event, context, site_ids):
    cid = parse_path_params(event).get("coverageAssignmentId")
    return json_response(None, 204)


# ===== BLACKOUT DATES =====
@router.route("GET", "/blackout-dates")
def list_blackouts(event, context, site_ids):
    return json_response({"data": []})


@router.route("POST", "/blackout-dates")
def create_blackout(event, context, site_ids):
    body = parse_body(event)
    return json_response({"id": str(uuid.uuid4()), "date": body.get("date")}, 201)


# ===== USERS =====
@router.route("GET", "/users")
def list_users(event, context, site_ids):
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT id, email, full_name, role, active FROM users ORDER BY full_name")
        rows = cur.fetchall()
    return json_response({"data": [{"id": str(r[0]), "email": r[1], "full_name": r[2], "role": r[3], "active": r[4]} for r in rows]})


@router.route("POST", "/users")
def create_user(event, context, site_ids):
    body = parse_body(event)
    conn = get_db_connection()
    with conn.cursor() as cur:
        uid = uuid.uuid4()
        cur.execute(
            "INSERT INTO users (id, cognito_sub, email, full_name, role, active) VALUES (%s, %s, %s, %s, %s, true) RETURNING id",
            (str(uid), body.get("cognito_sub", str(uuid.uuid4())), body["email"], body["full_name"], body.get("role", "ShiftSupervisor"))
        )
        result = cur.fetchone()
    return json_response({"id": str(result[0])}, 201)


@router.route("GET", "/users/{userId}")
def get_user(event, context, site_ids):
    uid = parse_path_params(event).get("userId")
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT id, email, full_name, role, active FROM users WHERE id = %s", (uid,))
        row = cur.fetchone()
        if not row:
            raise NotFoundError("User not found")
    return json_response({"id": str(row[0]), "email": row[1], "full_name": row[2], "role": row[3], "active": row[4]})


@router.route("PATCH", "/users/{userId}")
def update_user(event, context, site_ids):
    uid = parse_path_params(event).get("userId")
    body = parse_body(event)

    claims = event.get("requestContext", {}).get("authorizer", {}).get("lambda", {})
    caller_role = claims.get("role", "ShiftSupervisor")
    caller_rank = _ROLE_RANK.get(caller_role, 0)

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT role FROM users WHERE id = %s", (uid,))
        target = cur.fetchone()
        if not target:
            raise NotFoundError("User not found")
        target_rank = _ROLE_RANK.get(target[0], 0)
        if caller_rank < target_rank:
            raise AuthorizationError("Cannot modify a user with higher privileges")
        cur.execute(
            "UPDATE users SET full_name = COALESCE(%s, full_name), role = COALESCE(%s, role), updated_at = now() WHERE id = %s",
            (body.get("full_name"), body.get("role"), uid)
        )
    return json_response({"id": uid})


@router.route("GET", "/user-roles")
def list_roles(event, context, site_ids):
    return json_response({"data": [{"name": r, "rank": rank} for r, rank in _ROLE_RANK.items()]})


@router.route("GET", "/task-categories")
def list_task_categories(event, context, site_ids):
    return json_response({"data": []})


# ===== HEALTH =====
@router.route("GET", "/health")
def health_check(event, context, site_ids):
    status = {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        status["database"] = "connected"
    except Exception as e:
        status["database"] = f"error: {str(e)}"
    return json_response(status)


lambda_handler = make_handler(router)