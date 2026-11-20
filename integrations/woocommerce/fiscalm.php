<?php
/**
 * Plugin Name: fiscalm pentru WooCommerce
 * Description: Comenzi, facturi și starea SPV prin fiscalm.
 * Version: 0.1.0
 * Requires PHP: 8.1
 * Requires Plugins: woocommerce
 * WC requires at least: 11.2
 * WC tested up to: 11.2
 * License: GPL-2.0-or-later
 * Text Domain: fiscalm
 */

defined( 'ABSPATH' ) || exit;
require_once __DIR__ . '/includes/class-fiscalm-mapper.php';
require_once __DIR__ . '/includes/class-fiscalm-plugin.php';
add_action(
	'before_woocommerce_init',
	static function () {
		if ( class_exists( '\Automattic\WooCommerce\Utilities\FeaturesUtil' ) ) {
			\Automattic\WooCommerce\Utilities\FeaturesUtil::declare_compatibility( 'custom_order_tables', __FILE__, true );
		}
	}
);
add_action(
	'plugins_loaded',
	static function () {
		if ( class_exists( 'WooCommerce' ) ) {
			Fiscalm_Plugin::boot();
		}
	}
);
