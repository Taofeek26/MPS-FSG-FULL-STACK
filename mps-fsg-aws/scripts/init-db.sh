#!/bin/bash
# ============================================================================
# MPS FSG — Database Initialization
# Run against deployed RDS instance to apply full schema + RLS policies
# ============================================================================
# Usage:
#   ./scripts/init-db.sh <environment> [db-password]
#
#   If password not provided, retrieved from Secrets Manager automatically.
# ============================================================================

set -euo pipefail

ENV="${1:-prod}"
REGION="us-east-1"
SECRET_NAME="FSG-${ENV}-DatabaseCredentials"

echo "Computing RDS endpoint from CloudFormation outputs..."
DB_HOST=$(aws cloudformation describe-stacks --region "${REGION}" --stack-name "fsg-${ENV}" --query 'Stacks[0].Outputs[?OutputKey==`DatabaseEndpoint`].OutputValue' --output text)

if [ -z "${DB_HOST}" ] || [ "${DB_HOST}" = "None" ]; then
  echo "ERROR: Could not resolve database endpoint. Is the stack deployed?"
  exit 1
fi

echo "  Host: ${DB_HOST}"
echo "  Port: 5432"
echo "  DB:   fsg_production"

# Option 1: Retrieve credentials from Secrets Manager
echo ""
echo "Retrieving credentials from Secrets Manager (${SECRET_NAME})..."
SECRET_JSON=$(aws secretsmanager get-secret-value --region "${REGION}" --secret-id "${SECRET_NAME}" --query SecretString --output text)
DB_USER=$(echo "${SECRET_JSON}" | python3 -c "import sys,json; print(json.load(sys.stdin)['username'])")
DB_PASS=$(echo "${SECRET_JSON}" | python3 -c "import sys,json; print(json.load(sys.stdin)['password'])")

echo "  User: ${DB_USER}"
echo "  Connecting to initialize schema..."

# Apply schema via psql (must be installed: brew install libpq && echo 'export PATH="/opt/homebrew/opt/libpq/bin:$PATH"' >> ~/.zshrc)
PGPASSWORD="${DB_PASS}" psql \
  --host="${DB_HOST}" \
  --port=5432 \
  --username="${DB_USER}" \
  --dbname=fsg_production \
  --file=schema.sql

echo ""
echo "Schema initialization complete."