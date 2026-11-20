<?php
use PHPUnit\Framework\TestCase;

final class MappingTest extends TestCase {
	private function order( bool $inclusive = false, string $currency = 'RON', int $quantity = 2 ): WC_Order {
		$order = wc_create_order();
		$order->set_prices_include_tax( $inclusive );
		$order->set_currency( $currency );
		$order->set_billing_first_name( 'Client' );
		$order->set_billing_last_name( 'Sintetic' );
		$order->set_billing_country( 'RO' );
		$order->set_billing_city( 'Cluj-Napoca' );
		$order->set_billing_state( 'CJ' );
		$order->set_billing_address_1( 'Strada Exemplului 1' );
		$order->set_payment_method( 'cod' );
		$item = new WC_Order_Item_Product();
		$item->set_name( 'Produs sintetic' );
		$item->set_quantity( $quantity );
		$item->set_subtotal( '20.00' );
		$item->set_total( '18.00' );
		$item->set_taxes( array( 'total' => array( 1 => '3.78' ), 'subtotal' => array( 1 => '4.20' ) ) );
		$order->add_item( $item );
		$tax = new WC_Order_Item_Tax();
		$tax->set_rate_id( 1 ); $tax->set_rate_percent( '21' ); $tax->set_tax_total( '3.78' );
		$order->add_item( $tax );
		$order->set_total( '21.78' ); $order->set_status( 'processing' ); $order->save();
		return $order;
	}

	private function issued( WC_Order $order ): void {
		$response = wp_remote_post( 'http://api:8000/emit', array( 'timeout' => 30, 'headers' => array( 'Authorization' => 'Bearer ' . getenv( 'WC_TEST_TOKEN' ), 'Content-Type' => 'application/json' ), 'body' => wp_json_encode( array( 'type' => 'invoice.issued', 'external_id' => (string) $order->get_id(), 'order_id' => (string) $order->get_id(), 'channel' => 'synthetic-shop', 'company' => 'NORD' ) ) ) );
		self::assertSame( 200, wp_remote_retrieve_response_code( $response ) );
	}

	public function testMappingVatDiscountAndCurrency(): void {
		foreach ( array( false, true ) as $inclusive ) {
			$order = $this->order( $inclusive, 'EUR' );
			$body = Fiscalm_Mapper::order( $order, Fiscalm_Plugin::config() );
			self::assertSame( 'EUR', $body['currency'] );
			self::assertSame( $inclusive, $body['prices_include_vat'] );
			self::assertSame( $inclusive ? '12.100000' : '10.000000', $body['lines'][0]['unit_price'] );
			self::assertSame( $inclusive ? '-2.42' : '-2.00', $body['lines'][1]['unit_price'] );
			self::assertSame( '21.00', $body['lines'][0]['vat_rate'] );
			self::assertSame( 'person', $body['buyer']['type'] );
			self::assertFalse( $body['payment']['paid'] );
		}
	}

	public function testCompanyMappingAndShipping(): void {
		$order = $this->order();
		$order->update_meta_data( 'custom_cui', 'RO-SYNTHETIC' );
		$order->update_meta_data( 'custom_name', 'Firma Sintetică SRL' );
		$ship = new WC_Order_Item_Shipping();
		$ship->set_method_title( 'Livrare' ); $ship->set_total( '5.00' );
		$order->add_item( $ship ); $order->set_total( '26.78' ); $order->save();
		$body = Fiscalm_Mapper::order( $order, array( 'channel' => 'test', 'cui_keys' => 'custom_cui', 'company_keys' => 'custom_name' ) );
		self::assertSame( 'company', $body['buyer']['type'] );
		self::assertSame( 'RO-SYNTHETIC', $body['buyer']['cui'] );
		self::assertSame( '5.000000', $body['lines'][2]['unit_price'] );
	}

	public function testRoundingAndAmbiguousTotals(): void {
		self::assertSame( 101, Fiscalm_Mapper::scaled( '1.005' ) );
		self::assertSame( -101, Fiscalm_Mapper::scaled( '-1.005' ) );
		$order = $this->order( false, 'RON', 3 );
		self::assertSame( '6.666667', Fiscalm_Mapper::lines( $order )[0]['unit_price'] );
		$order->set_total( '21.79' );
		$this->expectException( RuntimeException::class );
		Fiscalm_Mapper::lines( $order );
	}

	public function testPartialAndFullRefunds(): void {
		foreach ( array( 1, 2 ) as $qty ) {
			$order = $this->order();
			$item = array_values( $order->get_items() )[0];
			$refund = wc_create_refund( array( 'order_id' => $order->get_id(), 'amount' => 10.89 * $qty, 'line_items' => array( $item->get_id() => array( 'qty' => $qty, 'refund_total' => 9 * $qty, 'refund_tax' => array( 1 => 1.89 * $qty ) ) ) ) );
			self::assertInstanceOf( WC_Order_Refund::class, $refund );
			$lines = Fiscalm_Mapper::lines( $refund, true );
			self::assertSame( '9.000000', $lines[0]['unit_price'] );
			self::assertSame( '21.00', $lines[0]['vat_rate'] );
		}
	}

	public function testQueueSendsResendsAwbAndRefund(): void {
		$order = $this->order();
		Fiscalm_Plugin::job( $order->get_id() );
		$order = wc_get_order( $order->get_id() );
		$this->issued( $order );
		self::assertSame( (string) $order->get_id(), $order->get_meta( '_fiscalm_id' ) );
		Fiscalm_Plugin::job( $order->get_id() );
		$order->update_meta_data( '_fan_courier_awb', 'SYNTHETIC-AWB' ); $order->save();
		Fiscalm_Plugin::job( $order->get_id(), 'awb' );
		$order = wc_get_order( $order->get_id() );
		self::assertSame( 'SYNTHETIC-AWB', $order->get_meta( '_fiscalm_awb' )['number'] );
		$item = array_values( $order->get_items() )[0];
		$refund = wc_create_refund( array( 'order_id' => $order->get_id(), 'amount' => '21.78', 'line_items' => array( $item->get_id() => array( 'qty' => 2, 'refund_total' => '18', 'refund_tax' => array( 1 => '3.78' ) ) ) ) );
		Fiscalm_Plugin::job( $order->get_id(), 'refund', $refund->get_id() );
		Fiscalm_Plugin::job( $order->get_id(), 'refund', $refund->get_id() );
		$calls = json_decode( wp_remote_retrieve_body( wp_remote_get( 'http://api:8000/calls' ) ), true );
		$refunds = array_filter( $calls, static fn( $call ) => str_ends_with( $call['path'], '/refund' ) );
		self::assertCount( 1, $refunds );
	}

	public function testWebhookAuthenticationAndBinding(): void {
		$order = $this->order(); Fiscalm_Plugin::job( $order->get_id() );
		$data = array( 'type' => 'invoice.issued', 'external_id' => (string) $order->get_id(), 'order_id' => (string) $order->get_id(), 'channel' => 'synthetic-shop', 'company' => 'NORD' );
		$body = wp_json_encode( $data );
		$request = new WP_REST_Request( 'POST' ); $request->set_body( $body );
		self::assertSame( 401, Fiscalm_Plugin::webhook( $request )->get_status() );
		foreach ( array( time() - 301 => 401, time() => 200 ) as $stamp => $expected ) {
			$request->set_header( 'x-fiscalm-timestamp', (string) $stamp );
			$request->set_header( 'x-fiscalm-signature', 'sha256=' . hash_hmac( 'sha256', $stamp . '.' . $body, getenv( 'WC_TEST_WEBHOOK_SECRET' ) ) );
			self::assertSame( $expected, Fiscalm_Plugin::webhook( $request )->get_status() );
		}
		$request->set_body( $body . ' ' );
		self::assertSame( 401, Fiscalm_Plugin::webhook( $request )->get_status() );
	}

	public function testTransportRefusesRedirectsAndRedactsErrors(): void {
		$order = $this->order();
		$filter = static function ( $pre, $args, $url ) {
			self::assertSame( 0, $args['redirection'] );
			self::assertTrue( hash_equals( 'Bearer ' . getenv( 'WC_TEST_TOKEN' ), $args['headers']['Authorization'] ) );
			return array( 'response' => array( 'code' => 302 ), 'body' => getenv( 'WC_TEST_TOKEN' ) . ' secret error', 'headers' => array() );
		};
		add_filter( 'pre_http_request', $filter, 10, 3 );
		try { Fiscalm_Plugin::job( $order->get_id() ); }
		finally { remove_filter( 'pre_http_request', $filter ); }
		$order = wc_get_order( $order->get_id() );
		self::assertFalse( str_contains( $order->get_meta( '_fiscalm_error' ), getenv( 'WC_TEST_TOKEN' ) ) );
		self::assertEmpty( $order->get_meta( '_fiscalm_id' ) );
	}

	public function testWrongCompanyWebhookIsRefused(): void {
		$order = $this->order(); Fiscalm_Plugin::job( $order->get_id() );
		$body = wp_json_encode( array( 'type' => 'invoice.issued', 'external_id' => (string) $order->get_id(), 'order_id' => (string) $order->get_id(), 'channel' => 'synthetic-shop', 'company' => 'STUDIO9' ) );
		$request = new WP_REST_Request( 'POST' ); $request->set_body( $body );
		$stamp = (string) time();
		$request->set_header( 'x-fiscalm-timestamp', $stamp );
		$request->set_header( 'x-fiscalm-signature', 'sha256=' . hash_hmac( 'sha256', $stamp . '.' . $body, getenv( 'WC_TEST_WEBHOOK_SECRET' ) ) );
		self::assertSame( 404, Fiscalm_Plugin::webhook( $request )->get_status() );
	}

	public function testFeeAndSingleParcelGlsMapping(): void {
		$order = $this->order();
		$fee = new WC_Order_Item_Fee(); $fee->set_name( 'Ambalare' ); $fee->set_amount( '2' ); $fee->set_total( '2' );
		$order->add_item( $fee ); $order->set_total( '23.78' );
		$order->update_meta_data( '_gls_tracking_codes', array( 'SYNTHETIC-PARCEL' ) ); $order->save();
		self::assertSame( '2.000000', Fiscalm_Mapper::lines( $order )[2]['unit_price'] );
		self::assertSame( 'SYNTHETIC-PARCEL', Fiscalm_Mapper::awb( $order, array() )['number'] );
	}

	public function testCompletedOrdersAreQueuedAndSent(): void {
		$order = $this->order();
		$order->update_status( 'completed' );
		$actions = as_get_scheduled_actions( array( 'hook' => 'fiscalm_job', 'args' => array( $order->get_id(), 'order', 0, 0 ), 'status' => 'pending' ), 'ids' );
		self::assertNotEmpty( $actions );
		ActionScheduler::runner()->process_action( $actions[0], 'fiscalm test' );
		self::assertNotEmpty( wc_get_order( $order->get_id() )->get_meta( '_fiscalm_id' ) );
	}

	public function testPaidRequiresGatewayEvidence(): void {
		$order = $this->order();
		foreach ( array( 'cod', 'bacs', 'cheque', 'custom_transfer', 'stripe' ) as $method ) {
			$order->set_payment_method( $method );
			$order->set_date_paid( null ); $order->set_transaction_id( '' );
			self::assertFalse( Fiscalm_Mapper::order( $order, Fiscalm_Plugin::config() )['payment']['paid'] );
			$order->set_date_paid( time() ); $order->set_transaction_id( 'SYNTHETIC-TRANSACTION' );
			self::assertSame( 'stripe' === $method, Fiscalm_Mapper::order( $order, Fiscalm_Plugin::config() )['payment']['paid'] );
		}
	}

	public function testCustomerEmailNeverContainsAdminError(): void {
		$order = $this->order();
		$order->update_meta_data( '_fiscalm_error', 'INTERNAL-SYNTHETIC-ERROR' ); $order->save();
		$admin = get_user_by( 'login', 'synthetic-admin' ); wp_set_current_user( $admin->ID );
		$settings = Fiscalm_Plugin::config(); update_option( 'fiscalm_settings', $settings + array( 'email_link' => true ), false );
		ob_start(); do_action( 'woocommerce_email_after_order_table', $order, false, false ); $html = ob_get_clean();
		self::assertStringNotContainsString( 'INTERNAL-SYNTHETIC-ERROR', $html );
		wp_set_current_user( 0 ); update_option( 'fiscalm_settings', $settings, false );
	}

	public function testRuntimeHttpIsRefusedUnlessExplicitTestFilter(): void {
		remove_all_filters( 'fiscalm_allow_test_http' );
		self::assertInstanceOf( WP_Error::class,  Fiscalm_Plugin::request( Fiscalm_Plugin::config(), '/orders/example' ) );
		add_filter( 'fiscalm_allow_test_http', static fn( $allow, $url ) => 'http://api:8000' === $url, 10, 2 );
	}

	public function testCalculatedWooCommerceTotals(): void {
		$rate = WC_Tax::_insert_tax_rate( array( 'tax_rate_country' => 'RO', 'tax_rate_state' => '', 'tax_rate' => '21.0000', 'tax_rate_name' => 'TVA', 'tax_rate_priority' => 1, 'tax_rate_compound' => 0, 'tax_rate_shipping' => 1, 'tax_rate_order' => 1, 'tax_rate_class' => '' ) );
		foreach ( array( 'yes', 'no' ) as $subtotal ) {
			update_option( 'woocommerce_tax_round_at_subtotal', $subtotal );
			$order = wc_create_order(); $order->set_billing_country( 'RO' ); $order->set_billing_state( 'CJ' );
			$product = new WC_Product_Simple(); $product->set_name( 'Produs calculat' ); $product->set_regular_price( '10.00' ); $product->save();
			$order->add_product( $product, 3 ); $order->calculate_totals(); $order->save();
			self::assertSame( '36.30', $order->get_total() );
			self::assertSame( '10.000000', Fiscalm_Mapper::lines( $order )[0]['unit_price'] );
		}
		WC_Tax::_delete_tax_rate( $rate );
	}

	public function testTaxRoundingDifferenceRefusesInvoice(): void {
		$order = $this->order();
		$item = array_values( $order->get_items() )[0];
		$item->set_quantity( 1 ); $item->set_subtotal( '0.03' ); $item->set_total( '0.03' );
		$item->set_taxes( array( 'total' => array( 1 => '0' ), 'subtotal' => array( 1 => '0' ) ) ); $item->save();
		$order->set_total( '0.03' ); $order->save();
		$this->expectException( RuntimeException::class );
		Fiscalm_Mapper::lines( $order );
	}

	public function testPdfOtherCustomerAndGuestAreDenied(): void {
		$order = $this->order(); $order->set_customer_id( 99999 ); $order->save();
		$_GET['order'] = $order->get_id();
		$handler = static fn() => static function ( $message, $title, $args ) { throw new RuntimeException( (string) $args['response'] ); };
		add_filter( 'wp_die_handler', $handler );
		try {
			foreach ( array( 0, 99998 ) as $uid ) {
				wp_set_current_user( $uid );
				try { Fiscalm_Plugin::pdf(); self::fail( 'Unauthorized PDF access succeeded' ); }
				catch ( RuntimeException $e ) { self::assertSame( '403', $e->getMessage() ); }
			}
		} finally { remove_filter( 'wp_die_handler', $handler ); wp_set_current_user( 0 ); unset( $_GET['order'] ); }
	}

	public function testCancelRecoversTimedOutSubmission(): void {
		$order = $this->order();
		$order->update_meta_data( '_fiscalm_binding', array_intersect_key( Fiscalm_Plugin::config(), array_flip( array( 'url', 'company', 'channel' ) ) ) );
		$order->update_meta_data( '_fiscalm_submitted', Fiscalm_Mapper::order( $order, Fiscalm_Plugin::config() ) ); $order->save();
		$order->update_status( 'cancelled' );
		Fiscalm_Plugin::job( $order->get_id(), 'cancel' );
		self::assertNotEmpty( wc_get_order( $order->get_id() )->get_meta( '_fiscalm_id' ) );
		$calls = json_decode( wp_remote_retrieve_body( wp_remote_get( 'http://api:8000/calls' ) ), true );
		self::assertSame( '/cancel', substr( end( $calls )['path'], -7 ) );
	}

	public function testWebhookBackOverHttpAndPdfProxy(): void {
		$order = $this->order(); Fiscalm_Plugin::job( $order->get_id() );
		$body = array( 'type' => 'invoice.issued', 'external_id' => (string) $order->get_id(), 'order_id' => (string) $order->get_id(), 'channel' => 'synthetic-shop', 'company' => 'NORD' );
		$response = wp_remote_post( 'http://api:8000/emit', array( 'timeout' => 30, 'headers' => array( 'Authorization' => 'Bearer ' . getenv( 'WC_TEST_TOKEN' ), 'Content-Type' => 'application/json' ), 'body' => wp_json_encode( $body ) ) );
		self::assertSame( 200, wp_remote_retrieve_response_code( $response ) );
		$order = wc_get_order( $order->get_id() );
		self::assertSame( 'accepted', $order->get_meta( '_fiscalm_spv_status' ) );
		self::assertNotEmpty( $order->get_meta( '_fiscalm_invoice_id' ) );
		$admin = get_user_by( 'login', 'synthetic-admin' );
		$cookies = array( new WP_Http_Cookie( array( 'name' => AUTH_COOKIE, 'value' => wp_generate_auth_cookie( $admin->ID, time() + 600, 'auth' ) ) ), new WP_Http_Cookie( array( 'name' => LOGGED_IN_COOKIE, 'value' => wp_generate_auth_cookie( $admin->ID, time() + 600, 'logged_in' ) ) ) );
		$pdf = wp_remote_get( 'http://shop/wp-admin/admin-post.php?action=fiscalm_pdf&order=' . $order->get_id(), array( 'cookies' => $cookies, 'redirection' => 0 ) );
		self::assertSame( 200, wp_remote_retrieve_response_code( $pdf ), (string) wp_remote_retrieve_header( $pdf, 'location' ) );
		self::assertSame( 'application/pdf', wp_remote_retrieve_header( $pdf, 'content-type' ) );
		self::assertStringStartsWith( '%PDF-', wp_remote_retrieve_body( $pdf ) );
	}

	public function testSettingsKeepSecretsAndRefuseHttp(): void {
		$settings = Fiscalm_Plugin::config();
		$admin = get_user_by( 'login', 'synthetic-admin' ); wp_set_current_user( $admin->ID );
		try {
			$_POST = array( 'fiscalm_save' => '1', '_wpnonce' => wp_create_nonce( 'fiscalm_settings' ), 'url' => 'https://fiscalm.example.invalid', 'company' => 'NORD', 'channel' => 'synthetic-shop', 'send_status' => 'processing', 'token' => '', 'webhook_secret' => '' );
			$_REQUEST['_wpnonce'] = $_POST['_wpnonce'];
			ob_start(); Fiscalm_Plugin::settings(); $html = ob_get_clean();
			self::assertTrue( hash_equals( $settings['token'], Fiscalm_Plugin::config()['token'] ) );
			self::assertFalse( str_contains( $html, $settings['token'] ) );
			$_POST['url'] = 'http://unsafe.example.invalid';
			$_REQUEST['_wpnonce'] = $_POST['_wpnonce'];
			ob_start(); Fiscalm_Plugin::settings(); $html = ob_get_clean();
			self::assertSame( 'https://fiscalm.example.invalid', Fiscalm_Plugin::config()['url'] );
			self::assertStringContainsString( 'Setările nu au fost salvate.', $html );
		} finally { $_POST = array(); $_REQUEST = array(); wp_set_current_user( 0 ); update_option( 'fiscalm_settings', $settings, false ); }
	}

	public function testUncertainRefundIsNotRetried(): void {
		$order = $this->order(); Fiscalm_Plugin::job( $order->get_id() ); $this->issued( $order );
		$item = array_values( $order->get_items() )[0];
		$refund = wc_create_refund( array( 'order_id' => $order->get_id(), 'amount' => '21.78', 'line_items' => array( $item->get_id() => array( 'qty' => 2, 'refund_total' => '18', 'refund_tax' => array( 1 => '3.78' ) ) ) ) );
		$count = 0;
		$filter = static function ( $pre, $args, $url ) use ( &$count ) { if ( ! str_ends_with( $url, '/refund' ) ) { return $pre; } ++$count; return new WP_Error( 'timeout', 'Synthetic timeout' ); };
		add_filter( 'pre_http_request', $filter, 10, 3 );
		try {
			Fiscalm_Plugin::job( $order->get_id(), 'refund', $refund->get_id() );
			Fiscalm_Plugin::job( $order->get_id(), 'refund', $refund->get_id() );
		} finally { remove_filter( 'pre_http_request', $filter ); }
		self::assertSame( 1, $count );
		self::assertNotEmpty( wc_get_order( $order->get_id() )->get_meta( '_fiscalm_error' ) );
	}

	public function testStackedAndCompoundTaxesAreRefused(): void {
		foreach ( array( true, false ) as $compound ) {
			$order = $this->order();
			if ( $compound ) { $tax = array_values( $order->get_items( 'tax' ) )[0]; $tax->set_compound( true ); $tax->save(); }
			else { $item = array_values( $order->get_items() )[0]; $item->set_taxes( array( 'total' => array( 1 => '1', 2 => '1' ), 'subtotal' => array( 1 => '1', 2 => '1' ) ) ); $item->save(); }
			try { Fiscalm_Mapper::lines( $order ); self::fail( 'Ambiguous tax accepted' ); }
			catch ( RuntimeException $e ) { self::assertNotEmpty( $e->getMessage() ); }
		}
	}

	public function testPartialQuantityRefundIsRefusedBeforeHttp(): void {
		$order = $this->order(); Fiscalm_Plugin::job( $order->get_id() );
		$item = array_values( $order->get_items() )[0];
		$refund = wc_create_refund( array( 'order_id' => $order->get_id(), 'amount' => '10.89', 'line_items' => array( $item->get_id() => array( 'qty' => 1, 'refund_total' => '9', 'refund_tax' => array( 1 => '1.89' ) ) ) ) );
		$order = wc_get_order( $order->get_id() );
		$this->expectException( RuntimeException::class );
		Fiscalm_Mapper::refund( $order, $refund );
	}

	public function testFailedAwbDoesNotRequeueAttemptZero(): void {
		foreach ( array( 422, 500 ) as $code ) {
			$order = $this->order(); Fiscalm_Plugin::job( $order->get_id() );
			$order = wc_get_order( $order->get_id() ); $order->update_meta_data( '_fan_courier_awb', 'SYNTHETIC-FAILED-AWB' ); $order->save();
			$args = array( $order->get_id(), 'awb', 0, 0 );
			Fiscalm_Plugin::enqueue( $order->get_id(), 'awb' );
			$pending = as_get_scheduled_actions( array( 'hook' => 'fiscalm_job', 'args' => $args, 'status' => 'pending' ), 'ids' );
			self::assertCount( 1, $pending );
			$filter = static fn( $pre, $options, $url ) => str_ends_with( $url, '/awb' ) ? array( 'response' => array( 'code' => $code ), 'body' => '{}', 'headers' => array() ) : $pre;
			add_filter( 'pre_http_request', $filter, 10, 3 );
			try { ActionScheduler::runner()->process_action( $pending[0], 'fiscalm failure test' ); }
			finally { remove_filter( 'pre_http_request', $filter ); }
			self::assertEmpty( as_get_scheduled_actions( array( 'hook' => 'fiscalm_job', 'args' => $args, 'status' => 'pending' ), 'ids' ) );
			$retry = as_get_scheduled_actions( array( 'hook' => 'fiscalm_job', 'args' => array( $order->get_id(), 'awb', 0, 1 ), 'status' => 'pending' ), 'ids' );
			self::assertCount( 500 === $code ? 1 : 0, $retry );
		}
	}

	public function testPlainEmailLinkKeepsQueryParameters(): void {
		$order = $this->order(); $order->set_customer_id( 1 ); $order->update_meta_data( '_fiscalm_invoice_id', 'synthetic-issued' ); $order->update_meta_data( '_fiscalm_invoice_number', 'TEST-1' ); $order->save();
		ob_start(); Fiscalm_Plugin::display( $order, true ); $plain = ob_get_clean();
		self::assertStringNotContainsString( '&amp;', $plain );
		$parts = explode( "\n", $plain ); parse_str( wp_parse_url( end( $parts ), PHP_URL_QUERY ), $query );
		self::assertSame( (string) $order->get_id(), $query['order'] );
	}

	public function testGrossRoundingPreservesOneNinetyNine(): void {
		$order = $this->order( true, 'RON', 1 );
		$item = array_values( $order->get_items() )[0];
		$item->set_subtotal( '1.644628' ); $item->set_total( '1.644628' );
		$item->set_taxes( array( 'total' => array( 1 => '0.345371900826446' ), 'subtotal' => array( 1 => '0.345371900826446' ) ) ); $item->save();
		$order->set_total( '1.99' ); $order->save();
		self::assertSame( '1.990000', Fiscalm_Mapper::lines( $order )[0]['unit_price'] );
	}

	public function testRejectedEditPreservesAcceptedRefundMap(): void {
		$order = $this->order(); Fiscalm_Plugin::job( $order->get_id() ); $this->issued( $order );
		$order = wc_get_order( $order->get_id() ); $map = $order->get_meta( '_fiscalm_line_map' );
		$item = array_values( $order->get_items() )[0];
		$item->set_subtotal( '40' ); $item->set_total( '38' ); $item->set_taxes( array( 'total' => array( 1 => '7.98' ), 'subtotal' => array( 1 => '8.40' ) ) ); $item->save();
		$order->set_total( '45.98' ); $order->save(); Fiscalm_Plugin::job( $order->get_id() );
		self::assertSame( $map, wc_get_order( $order->get_id() )->get_meta( '_fiscalm_line_map' ) );
		self::assertNotEmpty( wc_get_order( $order->get_id() )->get_meta( '_fiscalm_error' ) );
	}

	public function testRefundRefusesManuallyEditedIssuedInvoice(): void {
		$order = $this->order(); Fiscalm_Plugin::job( $order->get_id() ); $this->issued( $order );
		wp_remote_post( 'http://api:8000/invoice-version', array( 'headers' => array( 'Authorization' => 'Bearer ' . getenv( 'WC_TEST_TOKEN' ), 'Content-Type' => 'application/json' ), 'body' => wp_json_encode( array( 'external_id' => (string) $order->get_id(), 'version' => 2 ) ) ) );
		$item = array_values( $order->get_items() )[0];
		$refund = wc_create_refund( array( 'order_id' => $order->get_id(), 'amount' => '21.78', 'line_items' => array( $item->get_id() => array( 'qty' => 2, 'refund_total' => '18', 'refund_tax' => array( 1 => '3.78' ) ) ) ) );
		Fiscalm_Plugin::job( $order->get_id(), 'refund', $refund->get_id() );
		self::assertEmpty( wc_get_order( $refund->get_id() )->get_meta( '_fiscalm_attempted' ) );
		self::assertStringContainsString( 'verificată', wc_get_order( $order->get_id() )->get_meta( '_fiscalm_error' ) );
	}
}
