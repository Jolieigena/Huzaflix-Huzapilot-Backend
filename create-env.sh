#!/usr/bin/env bash
# Creates backend/.env from .env.example with fresh random secrets.
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
  echo ".env already exists, so I did not overwrite it." >&2
  exit 1
fi

rand() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$1"
  else
    python3 -c "import secrets; print(secrets.token_hex($1))"
  fi
}

sed \
  -e "s/^JWT_SECRET=.*/JWT_SECRET=$(rand 32)/" \
  -e "s/^API_KEY_AUTH_SECRET=.*/API_KEY_AUTH_SECRET=$(rand 32)/" \
  -e "s/^DB_PASSWORD=.*/DB_PASSWORD=$(rand 16)/" \
  -e "s/^AUTH_PASSWORD=.*/AUTH_PASSWORD=$(rand 16)/" \
  .env.example > .env
chmod 600 .env 2>/dev/null || true

echo "Created .env with new random secrets."
echo "Sign in as:  $(grep '^AUTH_USERNAME=' .env | cut -d= -f2)"
echo "Password:    see AUTH_PASSWORD in .env"
