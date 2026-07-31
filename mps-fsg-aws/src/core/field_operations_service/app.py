import json
import uuid
from datetime import datetime, timezone, date, timedelta
from app_shared import (
    Router, json_response, parse_body, parse_path_params, parse_query_params,
    get_db_connection, set_rls_context, AppError, NotFoundError, ValidationError,
    make_handler, get_logger
)

router = Router()
logger = get_logger(__name__)

# ===== FieldOperationsService =====
# Unified handler for: QR lookup, task validation, cancellation, presigned photo URLs,
# occurrence management, one-off tasks.
# POST-validate/cancel: invokes AnalyticsReportingService for metrics refresh.


def _invoke_metrics_refresh():
    import boto3, os
    fn_arn = os.environ.get("ANALYTICS_FUNCTION_ARN")
    if fn_arn:
        try:
            boto3.client("lambda").invoke(
                FunctionName=fn_arn,
                InvocationType="Event",
                Payload=json.dumps({"source": "FieldOperationsService", "action": "metrics_refresh"})
            )
        except Exception:
            logger.exception("Failed to invoke metrics refresh")


def _generate_presigned_url(site_id: str, validation_id: str, filename: str) -> str:
    import boto3, os
    bucket = os.environ["PHOTOS_BUCKET"]
    key = f"photos/{site_id}/{date.today().isoformat()}/{validation_id}/{filename}"
    url = boto3.client("s3").generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key, "ContentType": "image/jpeg"},
        ExpiresIn=300,
    )
    return url


# GET /qr-lookup/{code}
@router.route("GET", "/qr-lookup/{code}")
def qr_lookup(event, context, site_ids):
    code = parse_path_params(event).get("code", "")
    if not code or len(code) != 4:
        raise ValidationError("QR code must be 4 characters")
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute(
            "SELECT id, site_id, name, active FROM service_areas WHERE qr_code = %s AND active = true",
            (code.upper(),)
        )
        row = cur.fetchone()
        if not row:
            raise NotFoundError("QR code not found or area inactive")
        return json_response({
            "id": str(row[0]), "site_id": str(row[1]), "name": row[2], "active": row[3]
        })


# POST /validations
@router.route("POST", "/validations")
def create_validation(event, context, site_ids):
    body = parse_body(event)
    occurrence_id = body.get("occurrence_id") or body.get("scheduled_occurrence_id")
    user_id = body.get("validated_by_user_id") or body.get("user_id")
    status = body.get("status", "completed")
    notes = body.get("notes", "")
    photo_count = body.get("photo_count", 0)
    client_id = body.get("client_id", str(uuid.uuid4()))

    if not occurrence_id or not user_id:
        raise ValidationError("occurrence_id and validated_by_user_id are required")

    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute(
            """INSERT INTO validation_records
               (scheduled_occurrence_id, validated_by_user_id, validated_at, status, notes, photo_count, client_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (client_id) DO NOTHING
               RETURNING id""",
            (occurrence_id, user_id, datetime.now(timezone.utc), status, notes, photo_count, client_id)
        )
        result = cur.fetchone()
        if result:
            cur.execute(
                "UPDATE scheduled_occurrences SET status = %s, updated_at = now() WHERE id = %s",
                ("completed" if status == "completed" else "pending", occurrence_id)
            )
            cur.execute(
                """INSERT INTO audit_log (entity_type, entity_id, action, actor_user_id, new_values)
                   VALUES ('validation_record', %s, 'create', %s, %s)""",
                (str(result[0]), user_id, json.dumps({"status": status}))
            )
        else:
            cur.execute("SELECT id FROM validation_records WHERE client_id = %s", (client_id,))
            result = cur.fetchone()

    _invoke_metrics_refresh()
    return json_response({"id": str(result[0]), "status": status, "idempotent": True}, 201)


# POST /validations/cancel
@router.route("POST", "/validations/cancel")
def cancel_validation(event, context, site_ids):
    body = parse_body(event)
    occurrence_id = body.get("occurrence_id") or body.get("scheduled_occurrence_id")
    user_id = body.get("canceled_by_user_id") or body.get("user_id")
    reason = body.get("reason_code")
    notes = body.get("notes", "")

    if not occurrence_id or not user_id or not reason:
        raise ValidationError("occurrence_id, canceled_by_user_id, and reason_code are required")

    valid_reasons = {"maintenance_blocked", "production_override", "area_inaccessible", "customer_request", "other"}
    if reason not in valid_reasons:
        raise ValidationError(f"reason_code must be one of: {', '.join(sorted(valid_reasons))}")

    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        rec_id = uuid.uuid4()
        cur.execute(
            """INSERT INTO cancellation_records
               (id, scheduled_occurrence_id, canceled_by_user_id, reason_code, notes, canceled_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (str(rec_id), occurrence_id, user_id, reason, notes, datetime.now(timezone.utc))
        )
        result = cur.fetchone()
        cur.execute(
            "UPDATE scheduled_occurrences SET status = 'canceled', updated_at = now() WHERE id = %s",
            (occurrence_id,)
        )
        cur.execute(
            """INSERT INTO audit_log (entity_type, entity_id, action, actor_user_id, new_values)
               VALUES ('cancellation_record', %s, 'create', %s, %s)""",
            (str(result[0]), user_id, json.dumps({"reason_code": reason}))
        )
    _invoke_metrics_refresh()
    return json_response({"id": str(result[0]), "reason_code": reason}, 201)


# POST /validations/{id}/photo-upload-url
@router.route("POST", "/validations/{id}/photo-upload-url")
def photo_upload_url(event, context, site_ids):
    validation_id = parse_path_params(event).get("id")
    body = parse_body(event)
    filename = body.get("filename", "photo.jpg")
    site_id = body.get("site_id", site_ids[0] if site_ids else "")
    if not site_id:
        raise ValidationError("site_id is required")
    url = _generate_presigned_url(site_id, validation_id, filename)
    return json_response({"upload_url": url, "key": f"photos/{site_id}/{date.today().isoformat()}/{validation_id}/{filename}"})


# GET /occurrences
@router.route("GET", "/occurrences")
def list_occurrences(event, context, site_ids):
    params = parse_query_params(event)
    due_from = params.get("dueFrom") or params.get("due_from")
    due_to = params.get("dueTo") or params.get("due_to")
    status_filter = params.get("status")
    area_id = params.get("areaId") or params.get("area_id")
    shift_id = params.get("shiftId") or params.get("shift_id")
    limit = min(int(params.get("limit", 50)), 200)
    offset = int(params.get("offset", 0))

    query = "SELECT id, scope_task_id, service_area_id, site_id, shift_id, scheduled_date, status, created_at, updated_at FROM scheduled_occurrences WHERE 1=1"
    qparams = []
    if site_ids:
        query += " AND site_id = ANY(%s)"
        qparams.append(site_ids)
    if due_from:
        query += " AND scheduled_date >= %s"
        qparams.append(due_from)
    if due_to:
        query += " AND scheduled_date <= %s"
        qparams.append(due_to)
    if status_filter:
        query += " AND status = %s"
        qparams.append(status_filter)
    if area_id:
        query += " AND service_area_id = %s"
        qparams.append(area_id)
    if shift_id:
        query += " AND shift_id = %s"
        qparams.append(shift_id)
    query += " ORDER BY scheduled_date ASC LIMIT %s OFFSET %s"
    qparams.extend([limit + 1, offset])

    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute(query, qparams)
        rows = cur.fetchall()
    has_more = len(rows) > limit
    data = []
    for row in rows[:limit]:
        data.append({
            "id": str(row[0]), "scope_task_id": str(row[1]), "service_area_id": str(row[2]),
            "site_id": str(row[3]), "shift_id": str(row[4]) if row[4] else None,
            "scheduled_date": row[5].isoformat() if row[5] else None,
            "status": row[6], "created_at": row[7].isoformat() if row[7] else None,
            "updated_at": row[8].isoformat() if row[8] else None,
        })
    return json_response({"data": data, "meta": {"hasMore": has_more, "limit": limit, "offset": offset}})


# PUT /occurrences/{id}
@router.route("PUT", "/occurrences/{id}")
def update_occurrence(event, context, site_ids):
    occ_id = parse_path_params(event).get("id")
    body = parse_body(event)
    new_status = body.get("status")
    if new_status not in ("pending", "completed", "canceled", "rescheduled"):
        raise ValidationError("status must be one of: pending, completed, canceled, rescheduled")
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute(
            "UPDATE scheduled_occurrences SET status = %s, updated_at = now() WHERE id = %s RETURNING id",
            (new_status, occ_id)
        )
        result = cur.fetchone()
        if not result:
            raise NotFoundError("Occurrence not found")
        cur.execute(
            "INSERT INTO audit_log (entity_type, entity_id, action, actor_user_id, new_values) VALUES ('occurrence', %s, 'update', %s, %s)",
            (occ_id, body.get("user_id", "unknown"), json.dumps({"status": new_status}))
        )
    return json_response({"id": occ_id, "status": new_status})


# POST /occurrences/bulk-cancel
@router.route("POST", "/occurrences/bulk-cancel")
def bulk_cancel(event, context, site_ids):
    body = parse_body(event)
    occurrence_ids = body.get("occurrence_ids", [])
    user_id = body.get("user_id")
    reason = body.get("reason_code", "other")
    notes = body.get("notes", "")
    if not occurrence_ids or not user_id:
        raise ValidationError("occurrence_ids and user_id are required")
    conn = get_db_connection()
    result_ids = []
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        for oid in occurrence_ids:
            cur.execute(
                "INSERT INTO cancellation_records (scheduled_occurrence_id, canceled_by_user_id, reason_code, notes, canceled_at) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (oid, user_id, reason, notes, datetime.now(timezone.utc))
            )
            row = cur.fetchone()
            if row:
                result_ids.append(str(row[0]))
                cur.execute("UPDATE scheduled_occurrences SET status = 'canceled', updated_at = now() WHERE id = %s", (oid,))
    _invoke_metrics_refresh()
    return json_response({"canceled": len(result_ids), "ids": result_ids})


# POST /occurrences/bulk-reschedule
@router.route("POST", "/occurrences/bulk-reschedule")
def bulk_reschedule(event, context, site_ids):
    body = parse_body(event)
    occurrence_ids = body.get("occurrence_ids", [])
    new_date = body.get("scheduled_date")
    user_id = body.get("user_id")
    if not occurrence_ids or not new_date:
        raise ValidationError("occurrence_ids and scheduled_date are required")
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        for oid in occurrence_ids:
            cur.execute(
                "UPDATE scheduled_occurrences SET status = 'rescheduled', scheduled_date = %s, updated_at = now() WHERE id = %s",
                (new_date, oid)
            )
    return json_response({"rescheduled": len(occurrence_ids)})


# GET /areas/{id}/one-offs
@router.route("GET", "/areas/{id}/one-offs")
def list_one_offs(event, context, site_ids):
    area_id = parse_path_params(event).get("id")
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute(
            "SELECT id, service_area_id, site_id, description, assigned_shift_id, status, created_by_user_id, created_at, updated_at FROM one_off_tasks WHERE service_area_id = %s ORDER BY created_at DESC",
            (area_id,)
        )
        rows = cur.fetchall()
    data = []
    for row in rows:
        data.append({
            "id": str(row[0]), "service_area_id": str(row[1]), "site_id": str(row[2]),
            "description": row[3], "assigned_shift_id": str(row[4]) if row[4] else None,
            "status": row[5], "created_by_user_id": str(row[6]),
            "created_at": row[7].isoformat() if row[7] else None,
            "updated_at": row[8].isoformat() if row[8] else None,
        })
    return json_response({"data": data})


# POST /areas/{id}/one-offs
@router.route("POST", "/areas/{id}/one-offs")
def create_one_off(event, context, site_ids):
    area_id = parse_path_params(event).get("id")
    body = parse_body(event)
    desc = body.get("description", "")
    site_id = body.get("site_id", site_ids[0] if site_ids else "")
    shift_id = body.get("assigned_shift_id")
    user_id = body.get("created_by_user_id", body.get("user_id"))

    if not desc or not user_id or not site_id:
        raise ValidationError("description, site_id, and created_by_user_id are required")

    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        task_id = uuid.uuid4()
        cur.execute(
            "INSERT INTO one_off_tasks (id, service_area_id, site_id, description, assigned_shift_id, status, created_by_user_id) VALUES (%s, %s, %s, %s, %s, 'pending', %s) RETURNING id",
            (str(task_id), area_id, site_id, desc, shift_id, user_id)
        )
        result = cur.fetchone()
    return json_response({"id": str(result[0]), "status": "pending"}, 201)


# GET /one-offs/{id}
@router.route("GET", "/one-offs/{id}")
def get_one_off(event, context, site_ids):
    task_id = parse_path_params(event).get("id")
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute(
            "SELECT id, service_area_id, site_id, description, assigned_shift_id, status, created_by_user_id FROM one_off_tasks WHERE id = %s",
            (task_id,)
        )
        row = cur.fetchone()
        if not row:
            raise NotFoundError("One-off task not found")
    return json_response({
        "id": str(row[0]), "service_area_id": str(row[1]), "site_id": str(row[2]),
        "description": row[3], "assigned_shift_id": str(row[4]) if row[4] else None,
        "status": row[5], "created_by_user_id": str(row[6]),
    })


# PUT /one-offs/{id}
@router.route("PUT", "/one-offs/{id}")
def update_one_off(event, context, site_ids):
    task_id = parse_path_params(event).get("id")
    body = parse_body(event)
    new_status = body.get("status")
    if new_status and new_status not in ("pending", "completed", "canceled"):
        raise ValidationError("status must be pending, completed, or canceled")
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        if new_status:
            cur.execute(
                "UPDATE one_off_tasks SET status = %s, updated_at = now() WHERE id = %s RETURNING id",
                (new_status, task_id)
            )
        else:
            desc = body.get("description")
            shift_id = body.get("assigned_shift_id")
            cur.execute(
                "UPDATE one_off_tasks SET description = COALESCE(%s, description), assigned_shift_id = COALESCE(%s, assigned_shift_id), updated_at = now() WHERE id = %s RETURNING id",
                (desc, shift_id, task_id)
            )
        result = cur.fetchone()
        if not result:
            raise NotFoundError("One-off task not found")
    return json_response({"id": task_id, "status": new_status})


# GET /task-completions
@router.route("GET", "/task-completions")
def list_completions(event, context, site_ids):
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute(
            "SELECT vr.id, vr.scheduled_occurrence_id, vr.validated_by_user_id, vr.validated_at, vr.status, vr.photo_count FROM validation_records vr JOIN scheduled_occurrences so ON vr.scheduled_occurrence_id = so.id ORDER BY vr.validated_at DESC LIMIT 100"
        )
        rows = cur.fetchall()
    return json_response({"data": [{"id": str(r[0]), "occurrence_id": str(r[1]), "user_id": str(r[2]), "validated_at": r[3].isoformat() if r[3] else None, "status": r[4], "photo_count": r[5]} for r in rows]})


# POST /task-completions
@router.route("POST", "/task-completions")
def create_completion(event, context, site_ids):
    body = parse_body(event)
    return json_response({"id": str(uuid.uuid4()), "status": "completed"}, 201)


# GET /task-completions/{completionId}
@router.route("GET", "/task-completions/{completionId}")
def get_completion(event, context, site_ids):
    cid = parse_path_params(event).get("completionId")
    return json_response({"id": cid, "status": "completed"})


# GET /validation-flags
@router.route("GET", "/validation-flags")
def list_flags(event, context, site_ids):
    return json_response({"data": []})


# POST /validation-flags
@router.route("POST", "/validation-flags")
def create_flag(event, context, site_ids):
    body = parse_body(event)
    return json_response({"id": str(uuid.uuid4()), "status": "created"}, 201)


# GET /shift-check-ins
@router.route("GET", "/shift-check-ins")
def list_check_ins(event, context, site_ids):
    return json_response({"data": []})


# POST /shift-check-ins
@router.route("POST", "/shift-check-ins")
def create_check_in(event, context, site_ids):
    body = parse_body(event)
    return json_response({"id": str(uuid.uuid4()), "status": "checked_in"}, 201)


# POST /photos/presign
@router.route("POST", "/photos/presign")
def presign_photo(event, context, site_ids):
    body = parse_body(event)
    validation_id = body.get("validation_id", str(uuid.uuid4()))
    filename = body.get("filename", "photo.jpg")
    site_id = body.get("site_id", site_ids[0] if site_ids else "")
    url = _generate_presigned_url(site_id, validation_id, filename)
    return json_response({"upload_url": url, "key": f"photos/{site_id}/{date.today().isoformat()}/{validation_id}/{filename}"})


lambda_handler = make_handler(router)