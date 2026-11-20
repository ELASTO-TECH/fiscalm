<?php
/** Test environment only: no email, payment gateway or external HTTP effects. */
add_filter( 'pre_wp_mail', '__return_false' );
add_filter( 'fiscalm_allow_test_http', static fn( $allow, $url ) => in_array( $url, array( 'http://api:8000', 'http://fiscalm:8790' ), true ), 10, 2 );
add_filter( 'pre_http_request', static function ( $pre, $args, $url ) {
	return in_array( wp_parse_url( $url, PHP_URL_HOST ), array( 'api', 'fiscalm', 'shop', '127.0.0.1' ), true ) ? $pre : new WP_Error( 'test_network_disabled', 'External network disabled in tests.' );
}, 1, 3 );
