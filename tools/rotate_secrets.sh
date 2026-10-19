#!/bin/bash
# Rotate the three passwords of a jolt host in one go: APP_PASSWORT (login),
# the database password of the role `jolt`, and - if it exists - the password of
# the read-only role `grafana_ro` (tools/grafana/setup.sql).
#
# Run on the host, from your machine:
#
#     ssh root@host 'DRY_RUN=1 bash -s' < tools/rotate_secrets.sh     # only show
#     ssh root@host 'bash -s' < tools/rotate_secrets.sh               # do it
#
# Refuses to run while a trip is being recorded (the app restarts), writes the
# new .env next to the old one (`.env.bak-<time>`), changes the database first
# and the file second, and prints the two passwords you have to remember once at
# the end. Sessions that are already logged in stay logged in; the logger
# tokens are not touched.
set -euo pipefail

cd "${JOLT_DIR:-/opt/jolt}"
DRY_RUN="${DRY_RUN:-}"

[ -f .env ] || { echo ".env not found in $PWD" >&2; exit 1; }

running=$(docker exec jolt-db psql -U jolt -d jolt -tAc "select count(*) from live_sessions where running" | tr -d '[:space:]')
if [ "$running" != "0" ]; then
  echo "A trip is being recorded ($running running). Try again afterwards." >&2
  exit 75
fi

secret() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-24; }
NEW_APP=$(secret)
NEW_DB=$(secret)
NEW_GRAFANA=$(secret)

has_grafana=$(docker exec jolt-db psql -U jolt -d jolt -tAc "select count(*) from pg_roles where rolname='grafana_ro'" | tr -d '[:space:]')

echo "will change: APP_PASSWORT, DB_PASSWORD (role jolt)$([ "$has_grafana" = 1 ] && echo ", role grafana_ro")"
echo "then restart: jolt-app, jolt-backup (jolt-db keeps running)"
if [ -n "$DRY_RUN" ]; then
  echo "DRY_RUN: nothing changed."
  exit 0
fi

stamp=$(date +%Y%m%d-%H%M%S)
cp .env ".env.bak-$stamp"
chmod 600 ".env.bak-$stamp"

# The new file first, so a failed edit leaves the old one untouched.
umask 077
sed -E "s/^[[:space:]]*DB_PASSWORD=.*/DB_PASSWORD=$NEW_DB/; s/^[[:space:]]*APP_PASSWORT=.*/APP_PASSWORT=$NEW_APP/" .env > ".env.new"
grep -q "^DB_PASSWORD=$NEW_DB\$" .env.new && grep -q "^APP_PASSWORT=$NEW_APP\$" .env.new \
  || { rm -f .env.new; echo "could not edit .env, nothing changed" >&2; exit 1; }

docker exec -i jolt-db psql -U jolt -d jolt -v ON_ERROR_STOP=1 -v pw="$NEW_DB" <<< "ALTER ROLE jolt PASSWORD :'pw';"
if [ "$has_grafana" = 1 ]; then
  docker exec -i jolt-db psql -U jolt -d jolt -v ON_ERROR_STOP=1 -v pw="$NEW_GRAFANA" <<< "ALTER ROLE grafana_ro PASSWORD :'pw';"
fi

mv .env.new .env
docker compose up -d --no-deps jolt-app jolt-backup

echo
echo "done. Old file: $PWD/.env.bak-$stamp (delete it once everything works)."
echo "New login password (APP_PASSWORT): $NEW_APP"
[ "$has_grafana" = 1 ] && echo "New password of grafana_ro (enter it in the Grafana data source): $NEW_GRAFANA"
exit 0
