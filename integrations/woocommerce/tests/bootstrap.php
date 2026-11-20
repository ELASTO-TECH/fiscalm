<?php
require '/var/www/html/wp-load.php';
require_once ABSPATH . 'wp-admin/includes/template.php';
add_filter( 'fiscalm_allow_test_http', static fn( $allow, $url ) => 'http://api:8000' === $url, 10, 2 );
add_filter( 'pre_wp_mail', '__return_false' );
delete_transient( '_wc_activation_redirect' );
update_option( 'woocommerce_onboarding_profile', array( 'skipped' => true ) );
update_option( 'woocommerce_currency', 'RON' );
update_option( 'woocommerce_calc_taxes', 'yes' );
update_option( 'woocommerce_default_country', 'RO' );
update_option( 'fiscalm_settings', array( 'url' => 'http://api:8000', 'token' => getenv( 'WC_TEST_TOKEN' ), 'company' => 'NORD', 'channel' => 'synthetic-shop', 'send_status' => 'processing', 'webhook_secret' => getenv( 'WC_TEST_WEBHOOK_SECRET' ) ), false );
