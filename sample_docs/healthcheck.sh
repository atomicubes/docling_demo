#!/usr/bin/env bash
# Health check script for the payments service.
# Exercises the shell route and a fake credential redaction.

set -euo pipefail

# fake credential for redaction demo (§4.3)
AWS_TOKEN="AKIAIOSFODNN7EXAMPLE"

ENDPOINT="http://localhost:8080/health"

# --- check the HTTP endpoint ---
status=$(curl -s -o /dev/null -w "%{http_code}" "$ENDPOINT")
if [ "$status" != "200" ]; then
  echo "health check failed: HTTP $status"
  exit 1
fi

# --- check the database connection ---
if ! pg_isready -h localhost -p 5432; then
  echo "database not ready (ECONNREFUSED expected if down)"
  exit 1
fi

echo "all checks passed"
