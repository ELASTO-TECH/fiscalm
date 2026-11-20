<?php
/** Server-side credentials, durable jobs and authenticated invoice access. */
final class Fiscalm_Plugin {
	private static array $working = array();
	public static function boot(): void {
		add_action( 'admin_menu', static fn() => add_submenu_page( 'woocommerce', 'fiscalm', 'fiscalm', 'manage_options', 'fiscalm', array( self::class, 'settings' ) ) );
		add_action(
			'woocommerce_order_status_changed',
			static function ( $id, $from, $to ) {
				if ( self::sendable( (string) $to ) ) {
					self::enqueue( (int) $id ); }
				if ( 'cancelled' === $to ) {
					self::enqueue( (int) $id, 'cancel' ); }
			},
			10,
			3
		);
		add_action( 'woocommerce_order_refunded', static fn( $id, $refund ) => self::enqueue( (int) $id, 'refund', (int) $refund ), 10, 2 );
		add_action(
			'woocommerce_after_order_object_save',
			static function ( $order ) {
				if ( $order instanceof WC_Order && empty( self::$working[ $order->get_id() ] ) && $order->get_meta( '_fiscalm_id' ) ) {
					self::enqueue( $order->get_id(), 'awb' ); }
			}
		);
		add_action(
			'updated_post_meta',
			static function ( $meta_id, $object_id, $key ) {
				$keys = array_merge( array( '_hgezlpfcr_awb_number', '_fan_courier_awb', 'fan_courier_awb', '_sameday_awb', 'sameday_awb', '_cargus_awb', 'cargus_awb', '_dpd_awb', 'dpd_awb', 'dpd_export_package_number', '_gls_tracking_codes', '_gls_awb', 'gls_awb' ), explode( ',', self::config()['awb_key'] ?? '' ) );
				if ( ! empty( self::$working[ $object_id ] ) || ! in_array( $key, $keys, true ) ) {
					return; }
				$order = wc_get_order( $object_id );
				if ( $order instanceof WC_Order && empty( self::$working[ $order->get_id() ] ) && $order->get_meta( '_fiscalm_id' ) ) {
					self::enqueue( $order->get_id(), 'awb' ); }
			},
			10,
			3
		);
		add_action(
			'fiscalm_awb_watch',
			static function ( $id, $cycle ) {
				$order = wc_get_order( $id );
				if ( ! $order instanceof WC_Order || $order->has_status( array( 'cancelled', 'refunded' ) ) || $order->get_meta( '_fiscalm_awb' ) ) {
					return; }
				self::enqueue( (int) $id, 'awb' );
				if ( $cycle < 28 ) {
					as_schedule_single_action( time() + 6 * HOUR_IN_SECONDS, 'fiscalm_awb_watch', array( $id, $cycle + 1 ), 'fiscalm', true ); }
			},
			10,
			2
		);
		add_action( 'fiscalm_job', array( self::class, 'job' ), 10, 4 );
		add_action(
			'rest_api_init',
			static function () {
				register_rest_route(
					'fiscalm/v1',
					'/webhook',
					array(
						'methods'             => 'POST',
						'callback'            => array( self::class, 'webhook' ),
						'permission_callback' => '__return_true',
					)
				);
			}
		);
		add_action(
			'woocommerce_admin_order_data_after_order_details',
			static function ( $order ) {
				if ( $order->get_meta( '_fiscalm_error' ) ) {
					echo '<p>' . esc_html( $order->get_meta( '_fiscalm_error' ) ) . '</p>'; }
				self::display( $order );
			}
		);
		add_action( 'woocommerce_order_details_after_order_table', array( self::class, 'display' ) );
		add_action(
			'woocommerce_email_after_order_table',
			static function ( $order, $admin, $plain ) {
				if ( ! $admin && $order->get_customer_id() && ! empty( self::config()['email_link'] ) ) {
					self::display( $order, $plain ); }
			},
			10,
			3
		);
		add_action( 'admin_post_fiscalm_pdf', array( self::class, 'pdf' ) );
		add_action(
			'admin_post_nopriv_fiscalm_pdf',
			static function () {
				auth_redirect();
			}
		);
		add_action( 'woocommerce_order_action_fiscalm_retry', static fn( $order ) => self::enqueue( $order->get_id() ) );
		add_filter(
			'woocommerce_order_actions',
			static function ( $actions ) {
				$actions['fiscalm_retry'] = 'Trimite comanda în fiscalm';
				return $actions;
			}
		);
		add_action(
			'admin_notices',
			static function () {
				if ( current_user_can( 'manage_woocommerce' ) && empty( self::config()['token'] ) ) {
					echo '<div class="notice notice-warning"><p>' . esc_html( 'fiscalm nu este configurat. Completează setările din WooCommerce → fiscalm.' ) . '</p></div>'; }
			}
		);
	}

	private static function sendable( string $status ): bool {
		return 'completed' === $status || ( 'processing' === $status && 'processing' === ( self::config()['send_status'] ?? 'processing' ) );
	}

	public static function config(): array {
		return (array) get_option( 'fiscalm_settings', array() ); }

	public static function settings(): void {
		if ( ! current_user_can( 'manage_options' ) ) {
			return; }
		$s      = self::config();
		$labels = array(
			'url'            => 'Adresa fiscalm (HTTPS)',
			'token'          => 'Cheie API cu drepturi de scriere',
			'company'        => 'Codul firmei',
			'channel'        => 'Identificatorul magazinului',
			'send_status'    => 'Trimite comanda când ajunge în starea',
			'webhook_secret' => 'Secret pentru notificări',
			'cui_keys'       => 'Câmpuri CUI (separate prin virgulă)',
			'company_keys'   => 'Câmpuri pentru denumirea firmei (separate prin virgulă)',
			'awb_key'        => 'Câmp AWB suplimentar',
			'courier'        => 'Curier pentru câmpul suplimentar',
			'email_link'     => 'Adaugă linkul facturii în e-mailul comenzii',
		);
		if ( isset( $_POST['fiscalm_save'] ) ) {
			check_admin_referer( 'fiscalm_settings' );
			$next = $s;
			foreach ( $labels as $key => $label ) {
				$raw   = wp_unslash( $_POST[ $key ] ?? '' );
				$value = in_array( $key, array( 'token', 'webhook_secret' ), true ) ? trim( $raw ) : sanitize_text_field( $raw );
				if ( in_array( $key, array( 'token', 'webhook_secret' ), true ) && '' !== $value && ! preg_match( '/^[!-~]{32,512}$/D', $value ) ) {
					echo '<div class="notice notice-error"><p>Cheia sau secretul nu este valid. Folosește cel puțin 32 de caractere fără spații.</p></div>';
					return; }
				if ( in_array( $key, array( 'token', 'webhook_secret' ), true ) && '' === $value ) {
					continue; }
				$next[ $key ] = $value;
			}
			$url = wp_parse_url( $next['url'] );
			if ( ! $url || 'https' !== ( $url['scheme'] ?? '' ) || empty( $url['host'] ) || ( isset( $url['user'] ) || isset( $url['pass'] ) ) || isset( $url['query'] ) || isset( $url['fragment'] ) || ! in_array( $next['send_status'], array( 'processing', 'completed' ), true ) || ! preg_match( '/^[a-zA-Z0-9_-]+$/D', $next['company'] ) || ! preg_match( '/^[a-zA-Z0-9_-]+$/D', $next['channel'] ) ) {
				echo '<div class="notice notice-error"><p>Setările nu au fost salvate. Verifică adresa HTTPS, firma, magazinul și starea comenzii.</p></div>';
			} else {
				$next['email_link'] = 'yes' === $next['email_link'];
				update_option( 'fiscalm_settings', $next, false );
				$s = $next;
				echo '<div class="notice notice-success"><p>Setările au fost salvate.</p></div>';
			}
		}
		echo '<div class="wrap"><h1>fiscalm</h1><form method="post">';
		wp_nonce_field( 'fiscalm_settings' );
		foreach ( $labels as $key => $label ) {
			$secret = in_array( $key, array( 'token', 'webhook_secret' ), true );
			$value  = $secret ? '' : ( $s[ $key ] ?? ( 'send_status' === $key ? 'processing' : '' ) );
			if ( 'email_link' === $key ) {
				$value = ! empty( $s[ $key ] ) ? 'yes' : 'no'; }
			if ( 'send_status' === $key ) {
				echo '<p><label>' . esc_html( $label ) . '<br><select name="send_status"><option value="processing" ' . selected( $value, 'processing', false ) . '>În procesare</option><option value="completed" ' . selected( $value, 'completed', false ) . '>Finalizată</option></select></label></p>';
				continue; }
			if ( 'email_link' === $key ) {
				echo '<p><label><input type="checkbox" name="email_link" value="yes" ' . checked( $value, 'yes', false ) . '> ' . esc_html( $label ) . '</label></p>';
				continue; }
			echo '<p><label>' . esc_html( $label ) . '<br><input name="' . esc_attr( $key ) . '" type="' . ( $secret ? 'password' : 'text' ) . '" value="' . esc_attr( (string) $value ) . '" autocomplete="off"></label></p>';
		}
		echo '<p>Cheile salvate nu sunt afișate. Un câmp gol păstrează cheia existentă. Nu schimba magazinul și firma după ce ai trimis prima comandă.</p><p>Adresa pentru notificări: ' . esc_html( rest_url( 'fiscalm/v1/webhook' ) ) . '</p>';
		submit_button( 'Salvează setările', 'primary', 'fiscalm_save' );
		echo '</form></div>';
	}

	public static function enqueue( int $id, string $operation = 'order', int $refund = 0, int $attempt = 0 ): void {
		$args = array( $id, $operation, $refund, $attempt );
		if ( as_get_scheduled_actions(
			array(
				'hook'     => 'fiscalm_job',
				'args'     => $args,
				'group'    => 'fiscalm',
				'status'   => 'pending',
				'per_page' => 1,
			),
			'ids'
		) ) {
			return; }
		as_schedule_single_action( time() + ( $attempt ? min( 3600, 30 * 2 ** $attempt ) : 1 ), 'fiscalm_job', $args, 'fiscalm', false );
	}

	/** Never follow redirects carrying the bearer. Never expose response error text. */
	public static function request( array $binding, string $path, ?array $body = null ) {
		$s = self::config();
		if ( empty( $s['token'] ) || empty( $binding['url'] ) || ( $s['url'] ?? '' ) !== $binding['url'] || ( $s['company'] ?? '' ) !== $binding['company'] ) {
			return new WP_Error( 'configuration', 'fiscalm nu este configurat.' ); }
		if ( 'https' !== wp_parse_url( $binding['url'], PHP_URL_SCHEME ) && ! apply_filters( 'fiscalm_allow_test_http', false, $binding['url'] ) ) {
			return new WP_Error( 'configuration', 'Adresa fiscalm trebuie să folosească HTTPS.' ); }
		return wp_remote_request(
			rtrim( $binding['url'], '/' ) . '/api/v1/companies/' . rawurlencode( $binding['company'] ) . $path,
			array(
				'method'              => null === $body ? 'GET' : 'POST',
				'timeout'             => 25,
				'redirection'         => 0,
				'limit_response_size' => 10000000,
				'headers'             => array(
					'Authorization' => 'Bearer ' . $s['token'],
					'Content-Type'  => 'application/json',
				),
				'body'                => null === $body ? null : wp_json_encode( $body ),
			)
		);
	}

	public static function job( int $id, string $operation = 'order', int $refund_id = 0, int $attempt = 0 ): void {
		$order = wc_get_order( $id );
		if ( ! $order instanceof WC_Order ) {
			return; }
		if ( 'cancel' === $operation && ! $order->get_meta( '_fiscalm_id' ) && ! $order->get_meta( '_fiscalm_pending' ) && ! $order->get_meta( '_fiscalm_submitted' ) ) {
			return; }
		global $wpdb;
		// Connection-owned lock is released on a worker crash, without stale-lock deletion races.
		$lock = 'fiscalm_' . md5( $wpdb->prefix . ':' . $id );
		if ( '1' !== (string) $wpdb->get_var( $wpdb->prepare( 'SELECT GET_LOCK(%s, 0)', $lock ) ) ) {
			self::enqueue( $id, $operation, $refund_id, min( 8, $attempt + 1 ) );
			return; }
		self::$working[ $id ] = true;
		try {
			$order = wc_get_order( $id );
			if ( ! $order instanceof WC_Order ) {
				return; }
			$s       = self::config();
			$binding = $order->get_meta( '_fiscalm_binding' );
			if ( ! $binding ) {
				if ( empty( $s['url'] ) || empty( $s['company'] ) || empty( $s['channel'] ) ) {
					throw new RuntimeException( 'Completează setările fiscalm înainte de trimitere.' ); }
				$binding = array_intersect_key( $s, array_flip( array( 'url', 'company', 'channel' ) ) );
				$order->update_meta_data( '_fiscalm_binding', $binding );
				$order->save();
			}
			$remote = $order->get_meta( '_fiscalm_id' );
			$path   = '/orders';
			if ( 'order' === $operation ) {
				if ( ! $remote && ! self::sendable( $order->get_status() ) ) {
					throw new RuntimeException( 'Comanda nu a ajuns în starea aleasă pentru trimitere.' ); }
				$body = Fiscalm_Mapper::order( $order, $binding + $s );
				$order->update_meta_data( '_fiscalm_pending', $body );
				$pending_map = Fiscalm_Mapper::line_map( $order );
				$order->update_meta_data( '_fiscalm_pending_map', $pending_map );
				$order->save();
			} else {
				if ( ! $remote ) {
					$snapshot = $order->get_meta( '_fiscalm_pending' );
					$snapshot = $snapshot ? $snapshot : $order->get_meta( '_fiscalm_submitted' );
					if ( ! $snapshot ) {
						throw new RuntimeException( 'Comanda nu este confirmată în fiscalm. Trimite comanda înainte de această operație.' ); }
					$resolved = self::request( $binding, '/orders', $snapshot );
					$found    = is_wp_error( $resolved ) ? null : json_decode( wp_remote_retrieve_body( $resolved ), true );
					if ( ! is_array( $found ) || empty( $found['ok'] ) || empty( $found['id'] ) ) {
						if ( $attempt < 8 ) {
							self::enqueue( $id, $operation, $refund_id, $attempt + 1 ); }
						throw new RuntimeException( 'fiscalm nu a confirmat comanda. Verifică starea înainte de anulare sau rambursare.' );
					}
					$remote = (string) $found['id'];
					$order->update_meta_data( '_fiscalm_id', $remote );
					$order->save();
				}
				$path .= '/' . rawurlencode( (string) $remote ) . '/' . $operation;
				$body  = array();
				if ( 'awb' === $operation ) {
					$body = Fiscalm_Mapper::awb( $order, $s );
					if ( ! $body || $body === $order->get_meta( '_fiscalm_awb' ) ) {
						return; }
				}
				if ( 'refund' === $operation ) {
					$refund = wc_get_order( $refund_id );
					if ( ! $refund instanceof WC_Order_Refund || $refund->get_parent_id() !== $id ) {
						return; }
					if ( $refund->get_meta( '_fiscalm_attempted' ) ) {
						return; }
					if ( ! self::refresh( $order ) || ! $order->get_meta( '_fiscalm_invoice_id' ) || ! $order->get_meta( '_fiscalm_invoice_version' ) || (int) $order->get_meta( '_fiscalm_invoice_version' ) !== (int) $order->get_meta( '_fiscalm_draft_version' ) ) {
						throw new RuntimeException( 'Factura trebuie verificată în fiscalm înainte de rambursare.' ); }
					$body = Fiscalm_Mapper::refund( $order, $refund );
					// Contract has no refund idempotency key: an uncertain result MUST NOT be retried.
					$refund->update_meta_data( '_fiscalm_attempted', true );
					$refund->save();
				}
			}
			$response = self::request( $binding, $path, $body );
			$code     = is_wp_error( $response ) ? 0 : wp_remote_retrieve_response_code( $response );
			if ( $code < 200 || $code >= 300 ) {
				if ( 'refund' !== $operation && ( 0 === $code || 429 === $code || $code >= 500 ) && $attempt < 8 ) {
					self::enqueue( $id, $operation, $refund_id, $attempt + 1 ); }
				throw new RuntimeException( 'fiscalm nu a confirmat operația. Verifică legătura, cheia API și comanda în fiscalm înainte de retrimitere.' );
			}
			$data = json_decode( wp_remote_retrieve_body( $response ), true );
			if ( ! is_array( $data ) || empty( $data['ok'] ) || ( 'order' === $operation && empty( $data['id'] ) ) ) {
				throw new RuntimeException( 'Răspuns fiscalm nevalid. Verifică starea comenzii în fiscalm.' ); }
			if ( 'order' === $operation ) {
				$order->update_meta_data( '_fiscalm_id', (string) $data['id'] );
				$order->update_meta_data( '_fiscalm_submitted', $body );
				$order->update_meta_data( '_fiscalm_line_map', $pending_map );
				$order->delete_meta_data( '_fiscalm_pending' );
				$order->delete_meta_data( '_fiscalm_pending_map' );
				as_schedule_single_action( time() + 6 * HOUR_IN_SECONDS, 'fiscalm_awb_watch', array( $id, 0 ), 'fiscalm', true ); }
			if ( 'refund' === $operation && ! empty( $data['draft_id'] ) ) {
				$refund->update_meta_data( '_fiscalm_draft_id', (string) $data['draft_id'] );
				$refund->save(); }
			if ( 'awb' === $operation ) {
				$order->update_meta_data( '_fiscalm_awb', $body ); }
			$order->delete_meta_data( '_fiscalm_error' );
			$order->save();
			self::refresh( $order );
		} catch ( RuntimeException $e ) {
			$order->update_meta_data( '_fiscalm_error', $e->getMessage() );
			$order->save();
		} catch ( Throwable $e ) {
			$order->update_meta_data( '_fiscalm_error', 'Operația fiscalm necesită verificare. Verifică setările și comanda.' );
			$order->save();
		} finally {
			unset( self::$working[ $id ] );
			$wpdb->get_var( $wpdb->prepare( 'SELECT RELEASE_LOCK(%s)', $lock ) ); }
	}

	public static function refresh( $order ): bool {
		global $wpdb;
		$id   = $order->get_id();
		$lock = 'fiscalm_' . md5( $wpdb->prefix . ':' . $id );
		if ( '1' !== (string) $wpdb->get_var( $wpdb->prepare( 'SELECT GET_LOCK(%s, 0)', $lock ) ) ) {
			return false; }
		$was_working          = ! empty( self::$working[ $id ] );
		self::$working[ $id ] = true;
		try {
			$response = self::request( $order->get_meta( '_fiscalm_binding' ), '/orders/' . rawurlencode( (string) $order->get_meta( '_fiscalm_id' ) ) );
			if ( is_wp_error( $response ) || 200 !== wp_remote_retrieve_response_code( $response ) ) {
				return false; }
			$data = json_decode( wp_remote_retrieve_body( $response ), true );
			if ( ! is_array( $data ) ) {
				return false; }
			if ( isset( $data['spv']['status'] ) ) {
				$data['spv_status'] = $data['spv']['status']; }
			if ( ! empty( $data['invoice_id'] ) ) {
				$invoice = self::request( $order->get_meta( '_fiscalm_binding' ), '/invoicing/issued/' . rawurlencode( (string) $data['invoice_id'] ) );
				if ( ! is_wp_error( $invoice ) && 200 === wp_remote_retrieve_response_code( $invoice ) ) {
					$detail = json_decode( wp_remote_retrieve_body( $invoice ), true );
					if ( isset( $detail['version'] ) ) {
						$data['invoice_version'] = $detail['version']; }
					if ( isset( $detail['series'], $detail['number'] ) && is_scalar( $detail['series'] ) && is_scalar( $detail['number'] ) ) {
						$data['invoice_number'] = $detail['series'] . '-' . $detail['number']; }
				}
			}
			foreach ( array( 'invoice_id', 'invoice_number', 'status', 'spv_status', 'draft_version', 'invoice_version' ) as $field ) {
				if ( array_key_exists( $field, $data ) && ( null === $data[ $field ] || is_scalar( $data[ $field ] ) ) ) {
					$order->update_meta_data( '_fiscalm_' . $field, sanitize_text_field( (string) $data[ $field ] ) ); }
			}
			$order->save();
			return true;
		} finally {
			if ( ! $was_working ) {
				unset( self::$working[ $id ] ); }
			$wpdb->get_var( $wpdb->prepare( 'SELECT RELEASE_LOCK(%s)', $lock ) );
		}
	}

	public static function webhook( WP_REST_Request $request ) {
		$secret    = self::config()['webhook_secret'] ?? '';
		$stamp     = $request->get_header( 'x-fiscalm-timestamp' );
		$signature = $request->get_header( 'x-fiscalm-signature' );
		if ( str_starts_with( (string) $signature, 'sha256=' ) ) {
			$signature = substr( $signature, 7 ); }
		$body = $request->get_body();
		if ( ! $secret || strlen( $body ) > 65536 || ! ctype_digit( (string) $stamp ) || abs( time() - (int) $stamp ) > 300 || ! hash_equals( hash_hmac( 'sha256', $stamp . '.' . $body, $secret ), (string) $signature ) ) {
			return new WP_REST_Response( null, 401 ); }
		$data = json_decode( $body, true );
		if ( ! is_array( $data ) || ! in_array( $data['type'] ?? '', array( 'invoice.issued', 'invoice.spv_accepted', 'invoice.spv_rejected', 'invoice.collected', 'order.refused' ), true ) || ! preg_match( '/^[1-9][0-9]*$/D', (string) ( $data['external_id'] ?? '' ) ) ) {
			return new WP_REST_Response( null, 400 ); }
		$order   = wc_get_order( (int) $data['external_id'] );
		$binding = $order instanceof WC_Order ? $order->get_meta( '_fiscalm_binding' ) : array();
		if ( $binding && ( $data['channel'] ?? '' ) === $binding['channel'] && ( $data['company'] ?? '' ) === $binding['company'] && 'order.refused' === $data['type'] && null === ( $data['order_id'] ?? null ) ) {
			if ( ! $order->get_meta( '_fiscalm_id' ) ) {
				$order->update_meta_data( '_fiscalm_error', 'fiscalm a refuzat comanda. Verifică datele facturii.' );
				$order->save(); }
			return new WP_REST_Response( array( 'ok' => true ), 200 );
		}
		if ( ! $binding || ! $order->get_meta( '_fiscalm_id' ) || ( $data['channel'] ?? '' ) !== $binding['channel'] || ( $data['company'] ?? '' ) !== $binding['company'] || (string) ( $data['order_id'] ?? '' ) !== (string) $order->get_meta( '_fiscalm_id' ) ) {
			return new WP_REST_Response( null, 404 ); }
		// Fetch authoritative state: duplicate/out-of-order notifications cannot roll state back.
		return self::refresh( $order ) ? new WP_REST_Response( array( 'ok' => true ), 200 ) : new WP_REST_Response( null, 503 );
	}

	public static function display( $order, bool $plain = false ): void {
		if ( ! $order->get_customer_id() && ! current_user_can( 'manage_woocommerce' ) ) {
			return; }
		if ( ! $order->get_meta( '_fiscalm_invoice_id' ) ) {
			return; }
		// A login and ownership check, never an order key or bearer in an email URL.
		$url    = add_query_arg(
			array(
				'action' => 'fiscalm_pdf',
				'order'  => $order->get_id(),
			),
			admin_url( 'admin-post.php' )
		);
		$states = array(
			'accepted'  => 'Acceptată',
			'rejected'  => 'Respinsă',
			'uploaded'  => 'Trimisă',
			'uploading' => 'În curs de trimitere',
			'unknown'   => 'Necesită verificare',
			'error'     => 'Necesită verificare',
		);
		$label  = 'Factură ' . $order->get_meta( '_fiscalm_invoice_number' ) . ' | SPV: ' . ( $states[ $order->get_meta( '_fiscalm_spv_status' ) ] ?? 'În așteptare' );
		if ( $plain ) {
			// phpcs:ignore WordPress.Security.EscapeOutput.OutputNotEscaped -- plain-text email, not HTML.
			echo preg_replace( '/[\x00-\x1f\x7f]/', '', wp_strip_all_tags( $label ) ) . "\n" . esc_url_raw( $url );
		} else {
			echo '<p><a href="' . esc_url( $url ) . '">' . esc_html( $label ) . '</a></p>'; }
	}

	public static function pdf(): void {
		$order = wc_get_order( absint( $_GET['order'] ?? 0 ) );
		if ( ! $order instanceof WC_Order || ( ! current_user_can( 'manage_woocommerce' ) && ( ! get_current_user_id() || get_current_user_id() !== $order->get_customer_id() ) ) ) {
			wp_die( 'Acces refuzat.', '', array( 'response' => 403 ) ); }
		$id = $order->get_meta( '_fiscalm_invoice_id' );
		if ( ! $id ) {
			wp_die( 'Factura nu este disponibilă.', '', array( 'response' => 404 ) ); }
		$response = self::request( $order->get_meta( '_fiscalm_binding' ), '/invoicing/issued/' . rawurlencode( (string) $id ) . '/pdf' );
		if ( is_wp_error( $response ) || 200 !== wp_remote_retrieve_response_code( $response ) || ! str_starts_with( wp_remote_retrieve_body( $response ), '%PDF-' ) ) {
			wp_die( 'Factura nu este disponibilă. Încearcă din nou.', '', array( 'response' => 502 ) ); }
		nocache_headers();
		header( 'Content-Type: application/pdf' );
		header( 'Content-Disposition: inline; filename="factura.pdf"' );
		header( 'X-Content-Type-Options: nosniff' );
		// phpcs:ignore WordPress.Security.EscapeOutput.OutputNotEscaped -- authenticated PDF bytes.
		echo wp_remote_retrieve_body( $response );
		exit;
	}
}
