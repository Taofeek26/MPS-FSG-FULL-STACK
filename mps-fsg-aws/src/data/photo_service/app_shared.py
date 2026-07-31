import boto3
import json
import logging
import os
from datetime import datetime, timezone

_LOGGER = logging.getLogger(__name__)
_DB_CACHE = {}
_SECRET_CACHE = {}
_SECRET_CLIENT = None


class AppError(Exception):
    def __init__(self, message: str, status_code: int = 400, detail: dict = None):
        self.message = message
        self.status_code = status_code
        self.detail = detail or {}
        super().__init__(message)


class NotFoundError(AppError):
    def __init__(self, message: str = "Resource not found"):
        super().__init__(message, status_code=404)


class AuthorizationError(AppError):
    def __init__(self, message: str = "Forbidden"):
        super().__init__(message, status_code=403)


class ValidationError(AppError):
    def __init__(self, message: str = "Validation failed", detail: dict = None):
        super().__init__(message, status_code=422, detail=detail)


def get_logger(name: str = None) -> logging.Logger:
    logger = logging.getLogger(name or __name__)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            '{"timestamp":"%(asctime)s","level":"%(levelname)s","service":"%(name)s","message":"%(message)s"}'
        ))
        logger.addHandler(handler)
        logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    return logger


def get_secret(secret_name: str, region: str = None) -> dict:
    global _SECRET_CLIENT, _SECRET_CACHE
    cache_key = f"{region or os.environ.get('REGION', 'us-east-1')}:{secret_name}"
    if cache_key in _SECRET_CACHE:
        return _SECRET_CACHE[cache_key]
    if _SECRET_CLIENT is None:
        _SECRET_CLIENT = boto3.client("secretsmanager", region_name=region)
    response = _SECRET_CLIENT.get_secret_value(SecretId=secret_name)
    secret = json.loads(response["SecretString"])
    _SECRET_CACHE[cache_key] = secret
    return secret


def get_db_credentials() -> dict:
    secret_name = os.environ["DB_SECRET_NAME"]
    return get_secret(secret_name)


def get_db_connection():
    import psycopg2
    global _DB_CACHE
    cache_key = f"{os.environ['DB_HOST']}:{os.environ['DB_PORT']}:{os.environ['DB_NAME']}"
    if cache_key in _DB_CACHE:
        conn = _DB_CACHE[cache_key]
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            del _DB_CACHE[cache_key]
    creds = get_db_credentials()
    conn = psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=creds["username"],
        password=creds["password"],
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=3,
    )
    conn.autocommit = True
    _DB_CACHE[cache_key] = conn
    return conn


def set_rls_context(cursor, site_ids: list):
    if site_ids:
        cursor.execute(
            "SET LOCAL app.current_site_ids = %s",
            (",".join(str(sid) for sid in site_ids),)
        )


def get_site_ids_from_event(event: dict) -> list:
    allowed = []
    auth_claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    if not auth_claims:
        auth_claims = event.get("requestContext", {}).get("authorizer", {}).get("lambda", {})
    site_ids_str = auth_claims.get("site_ids", "") or auth_claims.get("custom:site_ids", "")
    if site_ids_str:
        allowed = site_ids_str.strip("[]").split(",")
        allowed = [s.strip().strip('"') for s in allowed if s.strip().strip('"')]
    x_site_id = None
    for header_name in ("x-site-id", "X-Site-Id"):
        x_site_id = event.get("headers", {}).get(header_name)
        if x_site_id:
            break
    if x_site_id:
        return [x_site_id] if x_site_id in allowed else []
    return allowed


def json_response(body, status_code: int = 200) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Site-Id,X-Allowed-Site-Ids",
            "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def error_response(error: AppError) -> dict:
    return json_response({"error": error.message, "detail": error.detail}, error.status_code)


def parse_path_params(event: dict) -> dict:
    return event.get("pathParameters") or {}


def parse_query_params(event: dict) -> dict:
    params = event.get("queryStringParameters") or {}
    return {k: v for k, v in params.items() if v is not None}


def parse_body(event: dict) -> dict:
    body = event.get("body")
    if body is None:
        return {}
    if isinstance(body, dict):
        return body
    return json.loads(body)


class Router:
    def __init__(self):
        self._routes = {}

    def route(self, method: str, path: str):
        def decorator(handler):
            self._routes[(method.upper(), path)] = handler
            return handler
        return decorator

    def dispatch(self, event: dict, context) -> dict:
        method = event.get("requestContext", {}).get("http", {}).get("method", event.get("httpMethod", "GET")).upper()
        raw_path = event.get("requestContext", {}).get("http", {}).get("path", event.get("rawPath", event.get("path", "/")))
        if method == "OPTIONS":
            return json_response({}, 204)
        site_ids = get_site_ids_from_event(event)
        handler = self._find_handler(method, raw_path)
        if handler is None:
            return error_response(NotFoundError(f"No route for {method} {raw_path}"))
        try:
            return handler(event, context, site_ids)
        except AppError as e:
            return error_response(e)
        except Exception as e:
            get_logger().exception("Unhandled error in route dispatch")
            return error_response(AppError("Internal server error", 500))

    def _find_handler(self, method: str, raw_path: str):
        direct_key = (method, raw_path)
        if direct_key in self._routes:
            return self._routes[direct_key]
        path_parts = raw_path.rstrip("/").split("/")
        for (r_method, r_path), handler in self._routes.items():
            if r_method != method:
                continue
            r_parts = r_path.rstrip("/").split("/")
            if len(r_parts) != len(path_parts):
                continue
            match = True
            for r_part, p_part in zip(r_parts, path_parts):
                if r_part.startswith("{") and r_part.endswith("}"):
                    continue
                if r_part != p_part:
                    match = False
                    break
            if match:
                return handler
        return None


def make_handler(router: Router):
    def lambda_handler(event, context):
        return router.dispatch(event, context)
    return lambda_handler