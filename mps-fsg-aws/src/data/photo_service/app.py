import json
from datetime import datetime, timezone
from app_shared import (
    Router, json_response,
    get_db_connection, make_handler, get_logger
)

router = Router()
logger = get_logger(__name__)

# ===== PhotoService =====
# Triggered by S3 ObjectCreated events on the photos bucket.
# Extracts photo metadata from S3 event and inserts photos table record.


def lambda_handler(event, context):
    results = []
    for record in event.get("Records", []):
        s3_info = record.get("s3", {})
        bucket = s3_info.get("bucket", {}).get("name", "")
        key = s3_info.get("object", {}).get("key", "")
        size = s3_info.get("object", {}).get("size", 0)

        logger.info(f"Processing photo upload: s3://{bucket}/{key}")

        key_parts = key.split("/")
        # Expected: photos/{site_id}/{date}/{validation_uuid}/{filename}
        date_str = key_parts[2] if len(key_parts) > 2 else None
        validation_id = key_parts[3] if len(key_parts) > 3 else None

        conn = get_db_connection()
        with conn.cursor() as cur:
            # Look up validation by key
            if validation_id:
                cur.execute(
                    "INSERT INTO photos (validation_record_id, s3_key, file_name, file_size_bytes, captured_at, uploaded_at) VALUES (%s::uuid, %s, %s, %s, %s::date, now()) ON CONFLICT DO NOTHING RETURNING id",
                    (validation_id, key, key_parts[-1] if key_parts else key, size, date_str)
                )
                result = cur.fetchone()
                if result:
                    cur.execute(
                        "UPDATE validation_records SET photo_count = photo_count + 1 WHERE id = %s",
                        (validation_id,)
                    )
                    logger.info(f"Photo registered: {str(result[0])}")
                    results.append({"id": str(result[0]), "key": key, "validation_id": validation_id})

    return {"statusCode": 200, "body": json.dumps({"processed": len(results)})}