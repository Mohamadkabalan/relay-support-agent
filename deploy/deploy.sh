#!/usr/bin/env bash
# shellcheck disable=SC2029,SC2087  # remote paths and versions expand here, on the local side, on purpose
# Deploy a released version to the VM (docs/DEPLOY.md):
#
#   deploy/deploy.sh <ssh-target> <version>      e.g. deploy/deploy.sh relay@203.0.113.10 1.2.0
#
# 1. Copy the compose file, the Caddyfile, the Postgres init script and the systemd units.
# 2. Set RELAY_VERSION in the server's .env, pull the images, start the stack and wait until
#    every service is healthy. The migrate job runs first (migrations and grants).
# 3. Smoke-check https://<RELAY_DOMAIN>/api/readyz from here.
#
# Rolling back is deploying the previous version. Migrations only move forward, so a rollback
# across a migration needs the database restored too (docs/DEPLOY.md, "Rolling back").
set -euo pipefail

target="${1:?usage: deploy/deploy.sh <ssh-target> <version>}"
version="${2:?usage: deploy/deploy.sh <ssh-target> <version>}"
version="${version#v}"
remote_dir="${RELAY_REMOTE_DIR:-/opt/relay}"
cd "$(dirname "$0")/.."

if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "deploy: version must look like 1.2.0, got $version" >&2
  exit 2
fi

echo "==> Copying the deployment files to $target:$remote_dir"
ssh "$target" "mkdir -p '$remote_dir/deploy/postgres/initdb' '$remote_dir/deploy/systemd'"
scp -q compose.prod.yaml "$target:$remote_dir/compose.prod.yaml"
scp -q deploy/Caddyfile "$target:$remote_dir/deploy/Caddyfile"
scp -q deploy/postgres/initdb/10-create-databases.sql "$target:$remote_dir/deploy/postgres/initdb/"
scp -q deploy/systemd/relay-demo-reset.service deploy/systemd/relay-demo-reset.timer \
  "$target:$remote_dir/deploy/systemd/"

echo "==> Starting version $version"
ssh "$target" bash -s <<REMOTE
set -euo pipefail
cd '$remote_dir'
test -f .env || { echo "deploy: $remote_dir/.env is missing (see deploy/env.prod.example)" >&2; exit 1; }
if grep -q '^RELAY_VERSION=' .env; then
  sed -i 's/^RELAY_VERSION=.*/RELAY_VERSION=$version/' .env
else
  echo 'RELAY_VERSION=$version' >> .env
fi
docker compose -f compose.prod.yaml pull --quiet
docker compose -f compose.prod.yaml up --detach --wait --remove-orphans
docker compose -f compose.prod.yaml ps
REMOTE

domain="$(ssh "$target" "grep '^RELAY_DOMAIN=' '$remote_dir/.env' | cut -d= -f2-")"
echo "==> Smoke check: https://$domain/api/readyz"
for attempt in 1 2 3 4 5 6; do
  if curl --fail --silent --show-error --max-time 10 "https://$domain/api/readyz"; then
    echo
    echo "==> Deployed $version to https://$domain"
    exit 0
  fi
  echo "    not ready yet (attempt $attempt)"
  sleep 10
done
echo "deploy: https://$domain/api/readyz never answered; check docker compose logs on the server" >&2
exit 1
