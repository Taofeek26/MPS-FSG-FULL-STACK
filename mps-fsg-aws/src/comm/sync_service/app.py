import json
import uuid
from datetime import datetime, timezone
from app_shared import (
    Router, json_response, parse_body,
    get_db_connection, set_rls_context, AppError, NotFoundError, ValidationError,
    make_handler, get_logger
)

router = Router()
logger = get_logger(__name__)

# ===== SyncService =====
# Idempotent batch sync of offline-captured validation records from mobile devices.


@router.route("POST", "/sync")
def sync_delta(event, context, site_ids):
    body = parse_body(event)
    records = body.get("records", [])
    user_id = body.get("user_id")
    device_id = body.get("device_id", "unknown")

    if not records:
        raise ValidationError("records array is required")

    job_id = uuid.uuid4()
    conn = get_db_connection()

    accepted = 0
    duplicates = 0
    failed = 0
    error_details = {}

    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)

        cur.execute(
            "INSERT INTO sync_jobs (id, user_id, status, records_submitted) VALUES (%s, %s, 'processing', %s)",
            (str(job_id), user_id, len(records))
        )

        for i, record in enumerate(records):
            client_id = record.get("client_id", str(uuid.uuid4()))
            occurrence_id = record.get("occurrence_id") or record.get("scheduled_occurrence_id")
            status = record.get("status", "completed")
            notes = record.get("notes", "")
            validated_at = record.get("validated_at", datetime.now(timezone.utc).isoformat())

            if not occurrence_id:
                failed += 1
                error_details[str(i)] = "missing occurrence_id"
                continue

            try:
                cur.execute(
                    """INSERT INTO validation_records
                       (scheduled_occurrence_id, validated_by_user_id, validated_at, status, notes, photo_count, client_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (client_id) DO NOTHING
                       RETURNING id""",
                    (occurrence_id, user_id, validated_at, status, notes, record.get("photo_count", 0), client_id)
                )
                result = cur.fetchone()
                if result:
                    accepted += 1
                    cur.execute(
                        "UPDATE scheduled_occurrences SET status = 'completed', updated_at = now() WHERE id = %s AND status != 'completed'",
                        (occurrence_id,)
                    )
                else:
                    duplicates += 1
            except Exception as e:
                failed += 1
                error_details[str(i)] = str(e)
                logger.exception(f"Sync record {i} failed: {e}")

        cur.execute(
            "UPDATE sync_jobs SET status = 'completed', processed_at = now(), records_accepted = %s, records_duplicate = %s, records_failed = %s, error_details = %s WHERE id = %s",
            (accepted, duplicates, failed, json.dumps(error_details), str(job_id))
        )

    return json_response({
        "job_id": str(job_id),
        "records_submitted": len(records),
        "records_accepted": accepted,
        "records_duplicate": duplicates,
        "records_failed": failed,
        "status": "completed",
    })


lambda_handler = make_handler(router)