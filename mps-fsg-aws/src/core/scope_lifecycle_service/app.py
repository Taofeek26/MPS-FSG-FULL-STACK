import json
import uuid
from datetime import datetime, timezone, timedelta, date
from app_shared import (
    Router, json_response, parse_body, parse_path_params,
    get_db_connection, set_rls_context, AppError, NotFoundError, ValidationError,
    make_handler, get_logger
)

router = Router()
logger = get_logger(__name__)

# ===== ScopeLifecycleService =====
# SOW Excel import, SchedulingEngine (occurrence calendar), mid-contract amendments.
# SchedulingEngine is a shared module within this service.


class SchedulingEngine:
    @staticmethod
    def generate_calendar(scope_task_id: str, site_id: str, contract_start: date, contract_end: date, frequency_per_year: int, shift_operating_days: list, blackout_dates: list) -> list:
        """Deterministic greedy algorithm: generate daily occurrence dates from SOW params."""
        if not contract_start or not contract_end:
            return []
        total_days = (contract_end - contract_start).days + 1
        if total_days <= 0:
            return []

        operating_day_numbers = set(shift_operating_days or [1, 2, 3, 4, 5])
        available_dates = []
        current = contract_start
        while current <= contract_end:
            if current.isoweekday() in operating_day_numbers and current not in blackout_dates:
                available_dates.append(current)
            current += timedelta(days=1)

        if not available_dates or frequency_per_year <= 0:
            return []

        occurrences = []
        available_count = len(available_dates)
        intervals = max(1, available_count // frequency_per_year)
        for i in range(frequency_per_year):
            idx = min(i * intervals, available_count - 1)
            occurrences.append(available_dates[idx])

        return sorted(set(occurrences))


def _get_site_blackout_dates(site_id: str) -> list:
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT blackout_date FROM blackout_dates WHERE site_id = %s", (site_id,))
        return [row[0] for row in cur.fetchall()]


# POST /scope:import
@router.route("POST", "/scope:import")
def import_scope(event, context, site_ids):
    body = parse_body(event)
    dry_run = body.get("dryRun", False)
    rows = body.get("rows", [])
    site_id = body.get("site_id", site_ids[0] if site_ids else "")
    user_id = body.get("user_id", "system")

    if not rows:
        raise ValidationError("rows is required")

    inserts, updates, skips, errors = [], [], [], []

    # Map: area_name → area_id
    area_map = {}
    conn = get_db_connection()
    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        for i, row in enumerate(rows):
            area_name = row.get("area_name") or row.get("areaName")
            task_name = row.get("task_name") or row.get("serviceTypeName")
            frequency = row.get("services_per_year") or row.get("servicesPerYear", 260)
            hours = row.get("time_per_task_hours") or row.get("timePerTaskHours", 0.5)

            if not area_name or not task_name:
                errors.append({"index": i, "message": "area_name and task_name are required"})
                continue

            try:
                frequency = int(frequency)
                hours = float(hours)
            except (ValueError, TypeError):
                errors.append({"index": i, "message": "Invalid numeric value for frequency or hours"})
                continue

            if area_name not in area_map:
                cur.execute("SELECT id FROM service_areas WHERE site_id = %s AND name = %s", (site_id, area_name))
                existing = cur.fetchone()
                if existing:
                    area_map[area_name] = str(existing[0])
                elif not dry_run:
                    area_id = uuid.uuid4()
                    cur.execute("INSERT INTO service_areas (id, site_id, name, qr_code) VALUES (%s, %s, %s, %s) ON CONFLICT (site_id, qr_code) DO NOTHING RETURNING id", (str(area_id), site_id, area_name, _generate_qr(site_id)))
                    result = cur.fetchone()
                    if result:
                        area_map[area_name] = str(result[0])
                        inserts.append({"type": "area", "name": area_name, "id": str(result[0])})
                else:
                    area_map[area_name] = "DRYRUN_" + area_name

            area_id = area_map.get(area_name)
            if not area_id:
                errors.append({"index": i, "message": f"Could not resolve area: {area_name}"})
                continue

            if not dry_run:
                task_id = uuid.uuid4()
                cur.execute(
                    "INSERT INTO scope_tasks (id, service_area_id, site_id, service_type_name, time_per_task_hours, services_per_year) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
                    (str(task_id), area_id, site_id, task_name, hours, frequency)
                )
                result = cur.fetchone()
                if result:
                    inserts.append({"type": "task", "name": task_name, "area": area_name, "id": str(result[0])})
                else:
                    updates.append({"type": "task", "name": task_name, "area": area_name})
            else:
                inserts.append({"type": "task", "name": task_name, "area": area_name, "id": "DRYRUN", "_dryRun": True})

    return json_response({"dryRun": dry_run, "inserts": len(inserts), "updates": len(updates), "skips": len(skips), "errors": errors})


def _generate_qr(site_id: str) -> str:
    import random, string
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=4))


# POST /task-schedules:regenerate
@router.route("POST", "/task-schedules:regenerate")
def regenerate_schedules(event, context, site_ids):
    body = parse_body(event)
    site_id = body.get("site_id", site_ids[0] if site_ids else "")

    if not site_id:
        raise ValidationError("site_id is required")

    conn = get_db_connection()
    blackouts = _get_site_blackout_dates(site_id)

    with conn.cursor() as cur:
        set_rls_context(cur, site_ids)
        cur.execute(
            "SELECT s.id, s.contract_start_date, s.contract_end_date FROM sites WHERE id = %s",
            (site_id,)
        )
        site_row = cur.fetchone()
        if not site_row:
            raise NotFoundError("Site not found")

        contract_start = site_row[1]
        contract_end = site_row[2]

        cur.execute(
            "SELECT st.id, st.services_per_year, st.shift_id, sh.operating_days FROM scope_tasks st LEFT JOIN shifts sh ON st.shift_id = sh.id WHERE st.site_id = %s AND st.active = true",
            (site_id,)
        )
        tasks = cur.fetchall()

        cur.execute("SELECT id, scope_task_id, scheduled_date FROM scheduled_occurrences WHERE site_id = %s AND status NOT IN ('completed', 'canceled')", (site_id,))
        existing_occurrences = {}
        for row in cur.fetchall():
            existing_occurrences.setdefault(str(row[1]), []).append((str(row[0]), row[2]))

        new_occurrences = []
        removed = []
        preserved = 0

        engine = SchedulingEngine()

        for task in tasks:
            task_id = str(task[0])
            frequency = task[1]
            shift_id = str(task[2]) if task[2] else None
            operating_days = task[3] if task[3] else [1, 2, 3, 4, 5]

            planned_dates = engine.generate_calendar(task_id, site_id, contract_start, contract_end, frequency, operating_days, blackouts)
            existing_dates = {d for _, d in existing_occurrences.get(task_id, [])}

            for planned_date in planned_dates:
                if planned_date not in existing_dates:
                    occ_id = uuid.uuid4()
                    cur.execute(
                        "INSERT INTO scheduled_occurrences (id, scope_task_id, service_area_id, site_id, shift_id, scheduled_date, status) SELECT %s, %s, sa.id, %s, %s, %s, 'pending' FROM scope_tasks st JOIN service_areas sa ON st.service_area_id = sa.id WHERE st.id = %s ON CONFLICT (scope_task_id, scheduled_date, shift_id) DO NOTHING",
                        (str(occ_id), task_id, site_id, shift_id, planned_date, task_id)
                    )
                    new_occurrences.append({"task_id": task_id, "date": planned_date.isoformat()})

            for occ_id, existing_date in existing_occurrences.get(task_id, []):
                if existing_date in planned_dates:
                    preserved += 1
                else:
                    cur.execute("UPDATE scheduled_occurrences SET status = 'superseded', updated_at = now() WHERE id = %s", (occ_id,))
                    removed.append(str(occ_id))

    result = {
        "new": len(new_occurrences),
        "removed": len(removed),
        "preserved": preserved,
    }
    return json_response(result)


lambda_handler = make_handler(router)