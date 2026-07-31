import json, os, boto3, psycopg2
from botocore.exceptions import ClientError
import cfnresponse

SQL = """
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS sites (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  name TEXT NOT NULL,
  facility_location TEXT,
  operating_days_per_week INTEGER DEFAULT 7,
  required_photo_percentage NUMERIC(4,2) DEFAULT 0.00,
  contract_start_date DATE,
  contract_end_date DATE,
  annual_contract_value NUMERIC(12,2),
  active BOOLEAN DEFAULT true,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS shifts (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  site_id UUID NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  start_time TIME NOT NULL,
  end_time TIME NOT NULL,
  operating_days INTEGER[] DEFAULT '{1,2,3,4,5}',
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS service_areas (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  site_id UUID NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  qr_code CHAR(4) NOT NULL,
  active BOOLEAN DEFAULT true,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (site_id, qr_code)
);

CREATE TABLE IF NOT EXISTS scope_tasks (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  service_area_id UUID NOT NULL REFERENCES service_areas(id) ON DELETE CASCADE,
  site_id UUID NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  shift_id UUID REFERENCES shifts(id) ON DELETE SET NULL,
  service_type_name TEXT NOT NULL,
  time_per_task_hours NUMERIC(5,2) NOT NULL DEFAULT 0.50,
  services_per_year INTEGER NOT NULL,
  active BOOLEAN DEFAULT true,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS scheduled_occurrences (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  scope_task_id UUID NOT NULL REFERENCES scope_tasks(id) ON DELETE CASCADE,
  service_area_id UUID NOT NULL,
  site_id UUID NOT NULL,
  shift_id UUID,
  scheduled_date DATE NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','completed','canceled','rescheduled','superseded')),
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (scope_task_id, scheduled_date, shift_id)
);
CREATE INDEX IF NOT EXISTS idx_occ_site_date_status ON scheduled_occurrences(site_id, scheduled_date, status);
CREATE INDEX IF NOT EXISTS idx_occ_area_status ON scheduled_occurrences(service_area_id, status);

CREATE TABLE IF NOT EXISTS users (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  cognito_sub TEXT UNIQUE NOT NULL,
  email TEXT UNIQUE NOT NULL,
  full_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('PlatformAdmin','SiteManager','LeadSupervisor','ShiftSupervisor')),
  active BOOLEAN DEFAULT true,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  last_login_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS user_site_assignments (
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  site_id UUID NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  assigned_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (user_id, site_id)
);

CREATE TABLE IF NOT EXISTS user_shift_assignments (
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  shift_id UUID NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
  assigned_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (user_id, shift_id)
);

CREATE TABLE IF NOT EXISTS validation_records (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  scheduled_occurrence_id UUID NOT NULL REFERENCES scheduled_occurrences(id),
  validated_by_user_id UUID NOT NULL REFERENCES users(id),
  validated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  status TEXT NOT NULL CHECK (status IN ('completed','partial')),
  notes TEXT,
  photo_count INTEGER DEFAULT 0,
  client_id UUID UNIQUE NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cancellation_records (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  scheduled_occurrence_id UUID NOT NULL REFERENCES scheduled_occurrences(id),
  canceled_by_user_id UUID NOT NULL REFERENCES users(id),
  reason_code TEXT NOT NULL CHECK (reason_code IN (
    'maintenance_blocked','production_override','area_inaccessible','customer_request','other'
  )),
  notes TEXT,
  canceled_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS one_off_tasks (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  service_area_id UUID NOT NULL REFERENCES service_areas(id),
  site_id UUID NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  description TEXT NOT NULL,
  assigned_shift_id UUID REFERENCES shifts(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','completed','canceled')),
  created_by_user_id UUID NOT NULL REFERENCES users(id),
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS one_off_validations (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  one_off_task_id UUID NOT NULL REFERENCES one_off_tasks(id) ON DELETE CASCADE,
  validated_by_user_id UUID NOT NULL REFERENCES users(id),
  validated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  status TEXT NOT NULL CHECK (status IN ('completed','partial')),
  notes TEXT,
  photo_count INTEGER DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS photos (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  validation_record_id UUID REFERENCES validation_records(id) ON DELETE SET NULL,
  one_off_validation_id UUID REFERENCES one_off_validations(id) ON DELETE SET NULL,
  s3_key TEXT NOT NULL,
  file_name TEXT,
  file_size_bytes BIGINT,
  captured_at DATE,
  uploaded_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS scope_imports (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  site_id UUID NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  imported_by_user_id UUID NOT NULL REFERENCES users(id),
  s3_key TEXT NOT NULL,
  original_filename TEXT,
  import_status TEXT NOT NULL DEFAULT 'success'
    CHECK (import_status IN ('success','partial','failed')),
  areas_created INTEGER DEFAULT 0,
  tasks_created INTEGER DEFAULT 0,
  occurrences_generated INTEGER DEFAULT 0,
  import_warnings JSONB DEFAULT '[]',
  imported_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS completion_metrics_cache (
  site_id UUID NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  cache_key TEXT NOT NULL,
  total_scheduled INTEGER NOT NULL DEFAULT 0,
  completed INTEGER NOT NULL DEFAULT 0,
  canceled INTEGER NOT NULL DEFAULT 0,
  pending INTEGER NOT NULL DEFAULT 0,
  completion_rate_pct NUMERIC(5,2) DEFAULT 0.00,
  scheduled_hours NUMERIC(10,2) DEFAULT 0,
  validated_hours NUMERIC(10,2) DEFAULT 0,
  canceled_hours NUMERIC(10,2) DEFAULT 0,
  cancellations_by_reason JSONB DEFAULT '{}',
  last_refreshed_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (site_id, cache_key)
);

CREATE TABLE IF NOT EXISTS sync_jobs (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id UUID NOT NULL REFERENCES users(id),
  submitted_at TIMESTAMPTZ DEFAULT now(),
  processed_at TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','processing','completed','failed')),
  records_submitted INTEGER NOT NULL DEFAULT 0,
  records_accepted INTEGER NOT NULL DEFAULT 0,
  records_duplicate INTEGER NOT NULL DEFAULT 0,
  records_failed INTEGER NOT NULL DEFAULT 0,
  error_details JSONB DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS device_tokens (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  platform TEXT NOT NULL CHECK (platform IN ('ios','android')),
  token TEXT NOT NULL,
  sns_endpoint_arn TEXT,
  active BOOLEAN DEFAULT true,
  registered_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  entity_type TEXT NOT NULL,
  entity_id UUID NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('create','update','delete')),
  actor_user_id UUID NOT NULL,
  old_values JSONB,
  new_values JSONB,
  ip_address INET,
  created_at TIMESTAMPTZ DEFAULT now()
) PARTITION BY RANGE (created_at);

-- Create current month audit_log partition
DO $$
DECLARE
  month_start DATE := date_trunc('month', now())::DATE;
  month_end DATE := (date_trunc('month', now()) + INTERVAL '1 month')::DATE;
  partition_name TEXT := 'audit_log_' || to_char(month_start, 'YYYY_MM');
BEGIN
  EXECUTE format(
    'CREATE TABLE IF NOT EXISTS %I PARTITION OF audit_log FOR VALUES FROM (%L) TO (%L)',
    partition_name, month_start, month_end
  );
END $$;

-- RLS: Site-scoped data isolation
ALTER TABLE sites ENABLE ROW LEVEL SECURITY;
ALTER TABLE shifts ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_areas ENABLE ROW LEVEL SECURITY;
ALTER TABLE scope_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE scheduled_occurrences ENABLE ROW LEVEL SECURITY;
ALTER TABLE validation_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE cancellation_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_off_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE one_off_validations ENABLE ROW LEVEL SECURITY;
ALTER TABLE photos ENABLE ROW LEVEL SECURITY;
ALTER TABLE scope_imports ENABLE ROW LEVEL SECURITY;
ALTER TABLE completion_metrics_cache ENABLE ROW LEVEL SECURITY;

-- RLS function: extract current site_ids from session
CREATE OR REPLACE FUNCTION current_site_ids()
RETURNS UUID[] AS $$
BEGIN
  RETURN string_to_array(
    NULLIF(current_setting('app.current_site_ids', true), ''),
    ','
  )::UUID[];
EXCEPTION WHEN OTHERS THEN
  RETURN NULL;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER;

-- RLS policies: allow if site_id is in current_site_ids, or user is PlatformAdmin
CREATE OR REPLACE FUNCTION site_is_accessible(check_site_id UUID)
RETURNS BOOLEAN AS $$
DECLARE
  ids UUID[];
BEGIN
  ids := current_site_ids();
  RETURN ids IS NULL OR check_site_id = ANY(ids);
END;
$$ LANGUAGE plpgsql STABLE;

CREATE OR REPLACE FUNCTION site_policy_condition(site_column TEXT)
RETURNS TEXT AS $$
BEGIN
  RETURN site_column || ' IS NULL OR site_is_accessible(' || site_column || ')';
END;
$$ LANGUAGE plpgsql IMMUTABLE;
"""

def lambda_handler(event, context):
    secret = boto3.client('secretsmanager').get_secret_value(SecretId=os.environ['DB_SECRET_NAME'])
    creds = json.loads(secret['SecretString'])

    conn = psycopg2.connect(
        host=os.environ['DB_HOST'],
        port=os.environ['DB_PORT'],
        dbname=os.environ['DB_NAME'],
        user=creds['username'],
        password=creds['password'],
        connect_timeout=10
    )
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(SQL)
    conn.close()

    cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
