<?php
use PHPUnit\Framework\TestCase;

/** Runs only when the dependency is present; never substitutes a mock for fiscalm. */
final class RealFiscalmTest extends TestCase {
	public function testOrderCreatesOneRealShadowDraftAndAwb(): void {
		$path = __DIR__ . '/../.runtime/connection.json';
		if ( ! file_exists( $path ) ) { self::markTestSkipped( 'Real fiscalm not started' ); }
		$settings = json_decode( file_get_contents( $path ), true );
		update_option( 'fiscalm_settings', $settings, false );
		add_filter( 'fiscalm_allow_test_http', static fn( $allow, $url ) => $allow || 'http://fiscalm:8790' === $url, 10, 2 );
		$order = wc_create_order();
		$order->set_billing_first_name( 'Client' ); $order->set_billing_last_name( 'Sintetic' );
		$order->set_billing_country( 'RO' ); $order->set_billing_state( 'CJ' );
		$order->set_billing_city( 'Cluj-Napoca' ); $order->set_billing_address_1( 'Strada Exemplului 2' );
		$order->set_payment_method( 'cod' ); $order->set_currency( 'RON' );
		$item = new WC_Order_Item_Product(); $item->set_name( 'Produs sintetic' );
		$item->set_quantity( 2 ); $item->set_subtotal( '20' ); $item->set_total( '18' );
		$item->set_taxes( array( 'total' => array( 1 => '3.78' ), 'subtotal' => array( 1 => '4.20' ) ) );
		$order->add_item( $item );
		$tax = new WC_Order_Item_Tax(); $tax->set_rate_id( 1 ); $tax->set_rate_percent( '21' ); $tax->set_tax_total( '3.78' );
		$order->add_item( $tax ); $order->set_total( '21.78' ); $order->set_status( 'processing' ); $order->save();
		$probe = Fiscalm_Plugin::request( $settings, '/orders', Fiscalm_Mapper::order( $order, $settings ) );
		$probe_data = json_decode( wp_remote_retrieve_body( $probe ), true );
		self::assertSame( 200, wp_remote_retrieve_response_code( $probe ), (string) ( $probe_data['error']['code'] ?? '' ) . ': ' . (string) ( $probe_data['error']['message'] ?? '' ) );
		Fiscalm_Plugin::job( $order->get_id() );
		$order = wc_get_order( $order->get_id() );
		self::assertEmpty( $order->get_meta( '_fiscalm_error' ) );
		$id = $order->get_meta( '_fiscalm_id' ); self::assertNotEmpty( $id );
		$response = Fiscalm_Plugin::request( $settings, '/orders/' . rawurlencode( $id ) );
		self::assertSame( 200, wp_remote_retrieve_response_code( $response ) );
		$first = json_decode( wp_remote_retrieve_body( $response ), true );
		self::assertSame( 'draft', $first['status'] );
		$draft = json_decode( wp_remote_retrieve_body( Fiscalm_Plugin::request( $settings, '/invoicing/drafts/' . rawurlencode( $first['draft_id'] ) ) ), true );
		self::assertSame( '21.78', $draft['totals']['total'] );
		$forbidden = Fiscalm_Plugin::request( $settings, '/invoicing/issue', array( 'id' => $first['draft_id'], 'version' => $draft['version'] ) );
		self::assertSame( 403, wp_remote_retrieve_response_code( $forbidden ) );
		Fiscalm_Plugin::job( $order->get_id() );
		$second = json_decode( wp_remote_retrieve_body( Fiscalm_Plugin::request( $settings, '/orders/' . rawurlencode( $id ) ) ), true );
		self::assertSame( $first['draft_id'], $second['draft_id'] );
		$order->update_meta_data( '_fan_courier_awb', 'SYNTHETIC-AWB' ); $order->save();
		Fiscalm_Plugin::job( $order->get_id(), 'awb' );
		$third = json_decode( wp_remote_retrieve_body( Fiscalm_Plugin::request( $settings, '/orders/' . rawurlencode( $id ) ) ), true );
		self::assertSame( 'SYNTHETIC-AWB', $third['awb']['number'] );
		$seed = wp_remote_post( 'http://fiscalm:8792/seed-issued', array( 'timeout' => 30, 'headers' => array( 'Authorization' => 'Bearer ' . $settings['token'], 'Content-Type' => 'application/json' ), 'body' => wp_json_encode( array( 'id' => $id ) ) ) );
		self::assertSame( 200, wp_remote_retrieve_response_code( $seed ) );
		self::assertTrue( Fiscalm_Plugin::refresh( $order ) );
		$pdf = Fiscalm_Plugin::request( $settings, '/invoicing/issued/' . rawurlencode( $first['draft_id'] ) . '/pdf' );
		self::assertSame( 200, wp_remote_retrieve_response_code( $pdf ) );
		self::assertSame( '%PDF-', substr( wp_remote_retrieve_body( $pdf ), 0, 5 ) );
		$refund = wc_create_refund( array( 'order_id' => $order->get_id(), 'amount' => '21.78', 'line_items' => array( $item->get_id() => array( 'qty' => 2, 'refund_total' => '18', 'refund_tax' => array( 1 => '3.78' ) ) ) ) );
		self::assertInstanceOf( WC_Order_Refund::class, $refund );
		Fiscalm_Plugin::job( $order->get_id(), 'refund', $refund->get_id() );
		$refund = wc_get_order( $refund->get_id() );
		$draft_id = $refund->get_meta( '_fiscalm_draft_id' );
		self::assertNotEmpty( $draft_id );
		$credit = json_decode( wp_remote_retrieve_body( Fiscalm_Plugin::request( $settings, '/invoicing/drafts/' . rawurlencode( $draft_id ) ) ), true );
		self::assertSame( '-21.78', $credit['totals']['total'] );
		Fiscalm_Plugin::job( $order->get_id(), 'refund', $refund->get_id() );
		self::assertSame( $draft_id, wc_get_order( $refund->get_id() )->get_meta( '_fiscalm_draft_id' ) );

	}
}
