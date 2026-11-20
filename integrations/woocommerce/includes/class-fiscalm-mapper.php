<?php
/** Order values come from WooCommerce's persisted totals, never current product prices. */
final class Fiscalm_Mapper {
	/** Parse a decimal without binary floating point. Reject unsupported precision. */
	public static function scaled( string $value, int $places = 2 ): int {
		if ( ! preg_match( '/^(-?)(\d{1,9})(?:\.(\d+))?$/D', $value, $m ) ) {
			throw new RuntimeException( 'Suma nu poate fi reprezentată. Verifică zecimalele comenzii.' );
		}
		$fraction = str_pad( $m[3] ?? '', 7, '0' );
		$number   = (int) $m[2] * ( 10 ** $places ) + (int) substr( $fraction, 0, $places );
		$number  += (int) $fraction[ $places ] >= 5 ? 1 : 0;
		return '-' === $m[1] ? -$number : $number;
	}

	public static function decimal( int $value, int $places = 2 ): string {
		return ( $value < 0 ? '-' : '' ) . intdiv( abs( $value ), 10 ** $places ) . '.' . str_pad( (string) ( abs( $value ) % ( 10 ** $places ) ), $places, '0', STR_PAD_LEFT );
	}

	private static function divide( int $cents, int $quantity ): int {
		if ( $cents > 9 * 10 ** 8 ) {
			throw new RuntimeException( 'Valoarea depășește limita de calcul.' ); }
		return intdiv( $cents * 10000000000 + intdiv( $quantity, 2 ), $quantity );
	}

	public static function meta( $order, string $keys ): string {
		foreach ( explode( ',', $keys ) as $key ) {
			$value = $order->get_meta( trim( $key ), true );
			if ( is_scalar( $value ) && '' !== trim( (string) $value ) ) {
				return trim( (string) $value );
			}
		}
		return '';
	}

	/** Map one tax rate per line; compound and stacked taxes require manual handling. */
	public static function lines( $order, bool $refund = false, ?array &$positions = null ): array {
		$positions = array();
		$rates     = array();
		$source    = $refund ? wc_get_order( $order->get_parent_id() ) : $order;
		foreach ( $source->get_items( 'tax' ) as $tax ) {
			if ( $tax->get_compound() ) {
				throw new RuntimeException( 'Taxele compuse necesită verificare manuală.' );
			}
			$rates[ $tax->get_rate_id() ] = (string) $tax->get_rate_percent();
		}
		$lines    = array();
		$expected = 0;
		foreach ( array( 'line_item', 'shipping', 'fee' ) as $kind ) {
			foreach ( $order->get_items( $kind ) as $item ) {
				$taxes = $item->get_taxes()['total'] ?? array();
				$ids   = array_keys( $taxes + ( $item->get_taxes()['subtotal'] ?? array() ) );
				if ( count( $ids ) > 1 || ( $ids && ! isset( $rates[ $ids[0] ] ) ) ) {
					throw new RuntimeException( 'Cota TVA nu poate fi stabilită. Verifică taxele comenzii.' );
				}
				$rate      = $ids ? $rates[ $ids[0] ] : '0';
				$total     = self::scaled( (string) $item->get_total() );
				$tax       = self::scaled( ( '' === (string) $item->get_total_tax() ? '0' : (string) $item->get_total_tax() ) );
				$expected += $total + $tax;
				$quantity  = 'line_item' === $kind ? abs( self::scaled( (string) $item->get_quantity(), 6 ) ) : 1000000;
				if ( 0 === $quantity ) {
					if ( 0 === $total && 0 === $tax ) {
						continue; }
					throw new RuntimeException( 'Rambursarea fără cantitate necesită verificare manuală.' );
				}
				// Refund endpoint receives positive quantities/prices for the returned portion.
				if ( $refund ) {
					$total = abs( $total );
					$tax   = abs( $tax ); }
				$inclusive = $source->get_prices_include_tax();
				$amount    = $total + ( $inclusive ? $tax : 0 );
				$unit      = self::divide( abs( $amount ), $quantity );
				$unit     *= $amount < 0 ? -1 : 1;
				$line      = array(
					'description' => $item->get_name(),
					'quantity'    => self::decimal( $quantity, 6 ),
					'unit'        => 'C62',
					'unit_price'  => self::decimal( $unit, 6 ),
					'vat_rate'    => self::decimal( self::scaled( $rate ) ),
				);
				if ( 'line_item' === $kind && $item->get_product() ) {
					$line['sku'] = $item->get_product()->get_sku(); }
				$lines[]                      = $line;
				$positions[ $item->get_id() ] = array(
					'positions' => array( count( $lines ) ),
					'quantity'  => $quantity,
					'net'       => $total,
					'tax'       => $tax,
					'kind'      => $kind,
				);
				// Discounts remain attached to their own VAT rate, as a separate negative line.
				if ( ! $refund && 'line_item' === $kind ) {
					$discount  = self::scaled( (string) $item->get_subtotal() ) - $total;
					$discount += $inclusive ? self::scaled( (string) $item->get_subtotal_tax() ) - $tax : 0;
					if ( $discount ) {
						$lines[ count( $lines ) - 1 ]['unit_price']  = self::decimal( self::divide( abs( $amount + $discount ), $quantity ), 6 );
						$lines[]                                     = array(
							'description' => 'Reducere: ' . $item->get_name(),
							'quantity'    => '1',
							'unit'        => 'C62',
							'unit_price'  => self::decimal( -$discount ),
							'vat_rate'    => $line['vat_rate'],
						);
						$positions[ $item->get_id() ]['positions'][] = count( $lines );
					}
				}
			}
		}
		if ( ! $lines || abs( $expected ) !== abs( self::scaled( (string) $order->get_total() ) ) ) {
			throw new RuntimeException( 'Totalul liniilor diferă de totalul comenzii. Verifică rotunjirile și rambursările.' );
		}
		$net    = 0;
		$gross  = 0;
		$groups = array();
		foreach ( $lines as $line ) {
			$price = self::scaled( $line['unit_price'], 6 );
			$qty   = self::scaled( $line['quantity'], 6 );
			$rate  = self::scaled( $line['vat_rate'] );
			// Bound multiplication before doing it; large orders require manual handling.
			if ( abs( $price ) > intdiv( PHP_INT_MAX, max( 1, abs( $qty ) ) * 10000 ) ) {
				throw new RuntimeException( 'Valoarea depășește limita de calcul.' ); }
			$numerator   = $price * $qty;
			$denominator = 10 ** 10;
			$gross      += intdiv( abs( $numerator ) + intdiv( $denominator, 2 ), $denominator ) * ( $numerator < 0 ? -1 : 1 );
			if ( $source->get_prices_include_tax() ) {
				$numerator   *= 10000;
				$denominator *= 10000 + $rate; }
			$cents           = intdiv( abs( $numerator ) + intdiv( $denominator, 2 ), $denominator ) * ( $numerator < 0 ? -1 : 1 );
			$net            += $cents;
			$groups[ $rate ] = ( $groups[ $rate ] ?? 0 ) + $cents;
		}
		$calculated = $net;
		foreach ( $groups as $rate => $amount ) {
			$calculated += intdiv( abs( $amount * $rate ) + 5000, 10000 ) * ( $amount < 0 ? -1 : 1 ); }
		if ( $source->get_prices_include_tax() ) {
			$calculated = $gross; }
		if ( abs( $expected ) !== $calculated && $expected !== $calculated ) {
			throw new RuntimeException( 'Totalul facturii ar diferi de comandă. Verifică rotunjirile TVA.' ); }
		return $lines;
	}

	public static function line_map( $order ): array {
		$map = array();
		self::lines( $order, false, $map );
		return $map;
	}

	/** The API accepts whole source-line positions, never guessed partial quantities. */
	public static function refund( $order, $refund ): array {
		$map = $order->get_meta( '_fiscalm_line_map' );
		if ( ! is_array( $map ) ) {
			throw new RuntimeException( 'Pozițiile facturii nu sunt disponibile. Verifică rambursarea în fiscalm.' ); }
		$selected = array();
		foreach ( array( 'line_item', 'shipping', 'fee' ) as $kind ) {
			foreach ( $refund->get_items( $kind ) as $item ) {
				$id       = $item->get_meta( '_refunded_item_id' );
				$original = $map[ $id ] ?? null;
				if ( ! $original || ( 'line_item' === $kind && abs( self::scaled( (string) $item->get_quantity(), 6 ) ) !== $original['quantity'] ) || abs( self::scaled( (string) $item->get_total() ) ) !== abs( $original['net'] ) || abs( self::scaled( (string) $item->get_total_tax() ) ) !== abs( $original['tax'] ) ) {
					throw new RuntimeException( 'Rambursarea unei părți dintr-o poziție necesită verificare în fiscalm. API-ul acceptă doar poziții întregi.' );
				}
				$selected = array_merge( $selected, $original['positions'] );
			}
		}
		if ( ! $selected ) {
			throw new RuntimeException( 'Rambursarea fără poziții necesită verificare în fiscalm.' ); }
		return array( 'lines' => array_values( array_unique( $selected ) ) );
	}

	public static function order( $order, array $settings ): array {
		if ( ! $order->get_date_created() ) {
			throw new RuntimeException( 'Comanda nu are data creării.' ); }
		$cui     = self::meta( $order, $settings['cui_keys'] ?? '_billing_cui,billing_cui,_billing_vat,billing_vat,_billing_vat_number' );
		$company = self::meta( $order, $settings['company_keys'] ?? '_billing_company,billing_company' );
		$company = $company ? $company : $order->get_billing_company();
		$method  = $order->get_payment_method();
		$county  = $order->get_billing_state();
		if ( 'RO' === $order->get_billing_country() && ! str_starts_with( $county, 'RO-' ) ) {
			$county = 'RO-' . $county; }
		$body = array(
			'channel'            => $settings['channel'],
			'external_id'        => (string) $order->get_id(),
			'placed_at'          => $order->get_date_created()->date( 'Y-m-d' ),
			'currency'           => $order->get_currency(),
			'prices_include_vat' => (bool) $order->get_prices_include_tax(),
			'buyer'              => array(
				'type'    => $company || $cui ? 'company' : 'person',
				'name'    => $company ? $company : trim( $order->get_billing_first_name() . ' ' . $order->get_billing_last_name() ),
				'email'   => $order->get_billing_email(),
				'address' => array(
					'street'  => trim( $order->get_billing_address_1() . ' ' . $order->get_billing_address_2() ),
					'city'    => $order->get_billing_city(),
					'county'  => $county,
					'country' => $order->get_billing_country(),
					'postal'  => $order->get_billing_postcode(),
				),
			),
			'lines'              => self::lines( $order ),
			'payment'            => array(
				'method'    => 'cod' === $method ? 'cod' : ( 'bacs' === $method ? 'transfer' : ( in_array( $method, array( 'stripe', 'woocommerce_payments', 'ppcp-gateway' ), true ) ? 'card' : 'other' ) ),
				'processor' => $method,
				'paid'      => $order->is_paid() && in_array( $method, array( 'stripe', 'woocommerce_payments', 'ppcp-gateway' ), true ) && null !== $order->get_date_paid() && '' !== $order->get_transaction_id(),
			),
		);
		if ( '' === $body['buyer']['email'] ) {
			unset( $body['buyer']['email'] ); }
		if ( '' === $body['buyer']['address']['postal'] ) {
			unset( $body['buyer']['address']['postal'] ); }
		foreach ( $body['lines'] as &$line ) {
			if ( isset( $line['sku'] ) && '' === $line['sku'] ) {
				unset( $line['sku'] ); }
		}
		unset( $line );
		if ( $cui ) {
			$body['buyer']['cui'] = $cui; }
		$awb = self::awb( $order, $settings );
		if ( $awb ) {
			$body['awb'] = $awb; }
		return $body;
	}

	public static function awb( $order, array $settings ): array {
		if ( ! empty( $settings['awb_key'] ) ) {
			$custom = self::meta( $order, $settings['awb_key'] );
			if ( $custom ) {
				return array(
					'courier' => $settings['courier'] ? $settings['courier'] : 'other',
					'number'  => $custom,
				); }
		}
		$repository = 'SamedayCourier\Shipping\Infrastructure\Wordpress\Sql\Repository\Sameday\SamedayAwbRepository';
		if ( class_exists( $repository ) ) {
			try {
				$record = ( new $repository() )->getAwbForOrderId( $order->get_id() );
			} catch ( Throwable $e ) {
				throw new RuntimeException( 'AWB-ul Sameday necesită verificare.' ); }
			if ( $record && $record->getAwbNumber() ) {
				return array(
					'courier' => 'Sameday',
					'number'  => $record->getAwbNumber(),
				); }
		}
		$tracking = $order->get_meta( '_gls_tracking_codes', true );
		if ( is_array( $tracking ) && 1 === count( $tracking ) && is_scalar( reset( $tracking ) ) ) {
			return array(
				'courier' => 'GLS',
				'number'  => (string) reset( $tracking ),
			);
		}
		$maps = array(
			'FAN Courier' => '_hgezlpfcr_awb_number,_fan_courier_awb,fan_courier_awb',
			'Sameday'     => '_sameday_awb,sameday_awb',
			'Cargus'      => '_cargus_awb,cargus_awb',
			'DPD'         => 'dpd_export_package_number,_dpd_awb,dpd_awb',
			'GLS'         => '_gls_awb,gls_awb',
		);
		if ( ! empty( $settings['awb_key'] ) ) {
			$maps = array( $settings['courier'] ? $settings['courier'] : 'other' => $settings['awb_key'] ) + $maps; }
		foreach ( $maps as $courier => $keys ) {
			$number = self::meta( $order, $keys );
			if ( $number ) {
				return array(
					'courier' => $courier,
					'number'  => $number,
				); }
		}
		return array();
	}
}
