#!/usr/bin/env bash
set -euo pipefail
if [ -z "${PS_VERSION:-}" ]; then
    for version in 8.2.1 9.0.1; do PS_VERSION="$version" "$0"; done
    exit 0
fi
cd "$(dirname "$0")/../tests"
export E2E_PASSWORD E2E_TOKEN
E2E_PASSWORD=$(openssl rand -hex 20)
E2E_TOKEN=$(openssl rand -hex 32)
project="fiscalm-prestashop-e2e-$$"
compose=(docker compose -p "$project" -f compose.yaml)
cleanup() { "${compose[@]}" down -v --remove-orphans --rmi local >/dev/null; }
trap cleanup EXIT
trap 'exit 130' INT TERM
"${compose[@]}" up -d --build >/dev/null
for attempt in $(seq 1 120); do
    if ! "${compose[@]}" ps --status running --services | grep -qx shop; then echo 'PrestaShop container stopped during installation' >&2; exit 1; fi
    if "${compose[@]}" exec -T shop test -f /var/www/html/app/config/parameters.php && "${compose[@]}" exec -T shop php -r 'require "/var/www/html/config/config.inc.php"; exit(Db::getInstance()->getValue("SELECT COUNT(*) FROM "._DB_PREFIX_."orders") > 0 ? 0 : 1);' >/dev/null 2>&1; then break; fi
    if [ "$attempt" = 120 ]; then echo 'PrestaShop installation timed out' >&2; exit 1; fi
    sleep 5
done
for attempt in $(seq 1 30); do
    if "${compose[@]}" exec -T shop curl -s --max-time 3 -o /dev/null http://localhost/; then break; fi
    if [ "$attempt" = 30 ]; then echo 'PrestaShop HTTP listener timed out' >&2; exit 1; fi
    sleep 1
done
"${compose[@]}" exec -T -e E2E_TOKEN="$E2E_TOKEN" shop php /tests/scenario.php
"${compose[@]}" exec -T fiscalm sh -c 'test ! -e /data/state/LIVE && python -c '\''import urllib.request; assert urllib.request.urlopen("http://127.0.0.1:8790/healthz").status == 200'\'''
"${compose[@]}" exec -T fiscalm python /tests/provision.py
"${compose[@]}" exec -T fiscalm cat /secrets/commerce.json | "${compose[@]}" exec -T shop sh -c 'umask 077; cat > /tmp/real-commerce.json'
"${compose[@]}" exec -T shop php /tests/real-prepare.php
"${compose[@]}" exec -T shop php /tests/real.php
"${compose[@]}" exec -T shop cat /tmp/real-order.json | "${compose[@]}" exec -T fiscalm python /tests/seed-issued.py
"${compose[@]}" exec -T shop php /tests/real-read.php
echo 'Real fiscalm commerce verified in isolated shadow mode.'
