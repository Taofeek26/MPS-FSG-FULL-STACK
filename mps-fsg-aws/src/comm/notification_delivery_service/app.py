import json
import uuid
import os
from datetime import datetime, timezone
from app_shared import (
    Router, json_response, parse_body,
    get_db_connection, set_rls_context, AppError, NotFoundError, make_handler, get_logger
)

router = Router()
logger = get_logger(__name__)

# ===== NotificationDeliveryService =====
# Push notifications (APNs/FCM via SNS) + scheduled report delivery (SES email).


def _get_platform_devices(user_ids: list, platform: str) -> list:
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT token, sns_endpoint_arn FROM device_tokens WHERE user_id = ANY(%s) AND platform = %s AND active = true",
            (user_ids, platform)
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def _send_push_notification(payload: dict, platform: str = "both"):
    import boto3
    sns = boto3.client("sns")
    user_ids = payload.get("user_ids", [])
    title = payload.get("title", "FSG Notification")
    body = payload.get("body", "")
    results = {"ios": 0, "android": 0}

    msg_structure = "json" if platform == "apns" else "json"
    msg_default = json.dumps({"title": title, "body": body})

    if platform in ("ios", "both"):
        for token, endpoint_arn in _get_platform_devices(user_ids, "ios"):
            if not endpoint_arn:
                continue
            try:
                msg = json.dumps({"default": msg_default, "APNS": json.dumps({"aps": {"alert": {"title": title, "body": body}}, "sound": "default"})})
                sns.publish(TargetArn=endpoint_arn, Message=msg, MessageStructure="json")
                results["ios"] += 1
            except Exception:
                logger.exception(f"Failed to send iOS push to {endpoint_arn}")

    if platform in ("android", "both"):
        for token, endpoint_arn in _get_platform_devices(user_ids, "android"):
            if not endpoint_arn:
                continue
            try:
                msg = json.dumps({"default": msg_default, "GCM": json.dumps({"notification": {"title": title, "body": body}})}) 
                sns.publish(TargetArn=endpoint_arn, Message=msg, MessageStructure="json")
                results["android"] += 1
            except Exception:
                logger.exception(f"Failed to send Android push to {endpoint_arn}")

    return results


def _send_email(to_addresses: list, subject: str, body_html: str):
    import boto3
    ses = boto3.client("ses", region_name=os.environ.get("SES_REGION", "us-east-1"))
    try:
        ses.send_email(
            Source=os.environ.get("SES_FROM_ADDRESS", "reports@mpsgroup.com"),
            Destination={"ToAddresses": to_addresses},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Html": {"Data": body_html, "Charset": "UTF-8"}},
            }
        )
        return True
    except Exception:
        logger.exception("Failed to send email via SES")
        return False


# EventBridge trigger: shift start notifications
def _handle_shift_start(event):
    payload = json.loads(event.get("Records", [{}])[0].get("body", "{}")) if "Records" in event else event
    shift_id = payload.get("shift_id")
    if not shift_id:
        return
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM user_shift_assignments WHERE shift_id = %s", (shift_id,))
        user_ids = [str(row[0]) for row in cur.fetchall()]
    if user_ids:
        _send_push_notification({"user_ids": user_ids, "title": "Shift Starting", "body": "Your shift is starting soon. Please check in."})


# EventBridge trigger: scheduled report delivery
def _handle_scheduled_report(event):
    recipients = payload.get("recipients", [])
    report_name = payload.get("report_name", "Weekly FSG Report")
    if not recipients:
        return
    html = f"<h2>{report_name}</h2><p>Please find attached the latest FSG completion report.</p><p>Generated: {datetime.now(timezone.utc).isoformat()}</p>"
    _send_email(recipients, report_name, html)


# POST /reports/deliveries
@router.route("POST", "/reports/deliveries")
def send_report_delivery(event, context, site_ids):
    body = parse_body(event)
    recipients = body.get("recipients", [])
    report_name = body.get("report_name", "Ad-Hoc FSG Report")
    success = False
    if recipients:
        html = f"<h2>{report_name}</h2><p>Here is your requested FSG report.</p><p>Generated: {datetime.now(timezone.utc).isoformat()}</p>"
        success = _send_email(recipients, report_name, html)
    return json_response({"id": str(uuid.uuid4()), "sent": success, "recipients": len(recipients)})


# GET /notifications
@router.route("GET", "/notifications")
def list_notifications(event, context, site_ids):
    return json_response({"data": []})


def lambda_handler(event, context):
    if "Records" in event:
        for record in event.get("Records", []):
            body = json.loads(record.get("body", "{}")) if isinstance(record.get("body"), str) else record.get("body", {})
            trigger_type = body.get("trigger_type", "")
            if trigger_type == "shift_start":
                _handle_shift_start(body)
            elif trigger_type == "scheduled_report":
                _handle_scheduled_report(body)
        return {}
    if "source" in event:
        threshold_data = event
        _send_push_notification({
            "user_ids": threshold_data.get("user_ids", []),
            "title": "Threshold Alert",
            "body": f"Completion rate dropped below threshold: {threshold_data.get('completion_rate')}%"
        })
        return {}
    return router.dispatch(event, context)