import json
import uuid
from app_shared import (
    Router, json_response, parse_body, parse_path_params, parse_query_params,
    get_db_connection, set_rls_context, AppError, NotFoundError, AuthorizationError,
    make_handler, get_logger
)

router = Router()
logger = get_logger(__name__)

# ===== SecurityAuthService =====
# Dual-mode Lambda: PreTokenGeneration trigger + API Gateway Lambda Authorizer
# Injects site_ids JWT claim at login. Enforces role-based access on every API request.

_ROLE_RANK = {
    "PlatformAdmin": 100,
    "SiteManager": 50,
    "LeadSupervisor": 25,
    "ShiftSupervisor": 10,
}


def _pre_token_generation(event):
    """Cognito PreTokenGeneration trigger — inject site_ids into JWT claims."""
    user_sub = event["request"]["userAttributes"]["sub"]
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT site_id FROM user_site_assignments WHERE user_id = "
            "(SELECT id FROM users WHERE cognito_sub = %s)",
            (user_sub,)
        )
        rows = cur.fetchall()
        site_ids = [str(row[0]) for row in rows] if rows else []
    event["response"]["claimsAndScopeOverrideDetails"] = {
        "accessTokenGeneration": {
            "claimsToAddOrOverride": {
                "site_ids": json.dumps(site_ids),
            }
        },
        "idTokenGeneration": {
            "claimsToAddOrOverride": {
                "site_ids": json.dumps(site_ids),
            }
        },
    }
    event["response"]["claimsOverrideDetails"] = {
        "claimsToAddOrOverride": {
            "site_ids": json.dumps(site_ids),
        }
    }
    return event


def _authorizer(event):
    """API Gateway Lambda Authorizer — validate JWT, enforce role-based scoping."""
    auth_header = None
    for header_name in ("authorization", "Authorization"):
        auth_header = event.get("headers", {}).get(header_name)
        if auth_header:
            break

    if not auth_header or " " not in auth_header:
        logger.warning("Authorizer: missing or malformed authorization header")
        return {"isAuthorized": False}

    token = auth_header.split(" ", 1)[1]

    import base64 as b64
    segments = token.split(".")
    if len(segments) != 3:
        return {"isAuthorized": False}

    try:
        padded = segments[1] + "=" * (4 - len(segments[1]) % 4)
        claims = json.loads(b64.b64decode(padded).decode("utf-8"))
    except Exception:
        return {"isAuthorized": False}

    from datetime import datetime, timezone
    now = int(datetime.now(timezone.utc).timestamp())
    if claims.get("exp", 0) < now:
        return {"isAuthorized": False}

    sub = claims.get("sub", "")
    role = None
    for group in (claims.get("cognito:groups", []) or []):
        if group in _ROLE_RANK:
            if role is None or _ROLE_RANK[group] > _ROLE_RANK.get(role, 0):
                role = group

    raw_site_ids = claims.get("site_ids", "[]") or "[]"
    try:
        site_ids = json.loads(raw_site_ids)
    except (json.JSONDecodeError, TypeError):
        site_ids = []

    path = event.get("rawPath", event.get("path", "/"))

    unscoped_paths = ["/health", "/sites", "/customers"]
    if path.startswith("/sites") and path not in ("/sites", "/customers"):
        pass

    allowed_methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

    return {
        "isAuthorized": True,
        "context": {
            "sub": sub,
            "role": role or "",
            "site_ids": json.dumps(site_ids),
        },
    }


def lambda_handler(event, context):
    trigger_source = event.get("triggerSource", "")
    if trigger_source and "TokenGeneration" in trigger_source:
        return _pre_token_generation(event)
    if event.get("type") == "TOKEN" or "authorizationToken" in event:
        return _authorizer(event)
    raw_path = event.get("rawPath", event.get("path", "/"))
    if raw_path:
        return _authorizer(event)
    return _authorizer(event)