#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .runtime
chmod 700 .runtime
exec 9>.runtime/e2e.lock
flock -n 9 || { echo "Another WooCommerce test run is active in this checkout." >&2; exit 1; }
export WC_FISCALM_SOURCE="${WC_FISCALM_SOURCE:-$(cd ../.. && pwd)}"
export WC_FISCALM_DOCKERFILE="$PWD/tests/fiscalm.Dockerfile"
export COMPOSE_PROJECT_NAME="fiscalm-wc-${USER:-test}-$$"
export WC_TEST_DB_PASSWORD WC_TEST_DB_ROOT_PASSWORD WC_TEST_TOKEN WC_TEST_WEBHOOK_SECRET
WC_TEST_DB_PASSWORD=$(openssl rand -hex 24)
WC_TEST_DB_ROOT_PASSWORD=$(openssl rand -hex 24)
WC_TEST_TOKEN=$(openssl rand -hex 24)
WC_TEST_WEBHOOK_SECRET=$(openssl rand -hex 24)
compose=(docker compose -f tests/compose.yml)
trap '"${compose[@]}" --profile real down --volumes --remove-orphans >/dev/null; rm -f .runtime/connection.json' EXIT
"${compose[@]}" up -d --build
shop() { "${compose[@]}" exec -T shop "$@"; }
for ((i=0; i<60; i++)); do
  if shop wp core is-installed --allow-root >/dev/null 2>&1; then break; fi
  if shop wp core install --allow-root --url=http://shop --title='Synthetic shop' --admin_user=synthetic-admin --admin_password=synthetic-local-only --admin_email=shop@example.invalid --skip-email >/dev/null 2>&1; then break; fi
  sleep 1
done
shop wp core update --version=7.0 --force --allow-root >/dev/null
shop wp plugin install woocommerce --version=11.2.1 --allow-root
shop mkdir -p wp-content/mu-plugins
shop cp wp-content/plugins/fiscalm/tests/mu-test-safety.php wp-content/mu-plugins/fiscalm-test-safety.php
shop wp plugin activate woocommerce --allow-root
shop wp plugin activate fiscalm --allow-root
shop wp option update woocommerce_custom_orders_table_enabled yes --allow-root
shop bash -c 'cd wp-content/plugins/fiscalm && composer install --no-interaction --quiet'
rm -f .runtime/unit.xml
shop bash -c 'cd wp-content/plugins/fiscalm && vendor/bin/phpunit --log-junit .runtime/unit.xml --bootstrap tests/bootstrap.php tests/MappingTest.php'
test -s .runtime/unit.xml
if [[ -f "$WC_FISCALM_SOURCE/src/fiscalm/commerce/orders.py" ]]; then
  mkdir -p .runtime
  chmod 700 .runtime
  rm -f .runtime/connection.json
  "${compose[@]}" --profile real up -d --build fiscalm
  for ((i=0; i<60; i++)); do
    [[ -f .runtime/connection.json ]] && break
    if "${compose[@]}" --profile real ps --all --status exited --services | rg -q '^fiscalm$'; then
      echo 'Isolated fiscalm failed during setup; inspect its container logs locally.' >&2
      exit 1
    fi
    sleep 1
  done
  test -f .runtime/connection.json
  rm -f .runtime/real.xml
  shop bash -c 'cd wp-content/plugins/fiscalm && vendor/bin/phpunit --log-junit .runtime/real.xml --bootstrap tests/bootstrap.php tests/RealFiscalmTest.php'
  test -s .runtime/real.xml
else
  echo 'Real fiscalm stage pending #137 (commerce.py absent); only contract fixture ran.'
fi

if [[ -n "${WC_CAPTURE_SCRIPT:-}" ]]; then
  port=$("${compose[@]}" port shop 80)
  shop wp option update siteurl "http://$port" --allow-root >/dev/null
  shop wp option update home "http://$port" --allow-root >/dev/null
  shop wp option delete fiscalm_settings --allow-root >/dev/null
  shop wp plugin deactivate fiscalm --allow-root >/dev/null
  node "$WC_CAPTURE_SCRIPT" "http://$port" before
  shop wp plugin activate fiscalm --allow-root >/dev/null
  node "$WC_CAPTURE_SCRIPT" "http://$port" after
fi
