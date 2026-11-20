<?php
/** SPDX-License-Identifier: AFL-3.0
 * Copyright 2026 fiscalm contributors. Licensed under the Academic Free License version 3.0. */
if (!defined('_PS_VERSION_')) { exit; }
require_once __DIR__ . '/classes/Mapper.php';
require_once __DIR__ . '/classes/Client.php';

class Fiscalm extends Module
{
    private ?int $activeShop = null;
    private const FIELDS = ['URL', 'TOKEN', 'COMPANY', 'CHANNEL', 'STATE', 'SECRET', 'AWB_FIELD'];

    public function __construct()
    {
        $this->name = 'fiscalm';
        $this->tab = 'billing_invoicing';
        $this->version = '1.0.0';
        $this->author = 'fiscalm';
        $this->bootstrap = true;
        parent::__construct();
        $this->displayName = 'fiscalm';
        $this->description = 'Facturare e-Factura și reconciliere bancară. Integrarea este gratuită în toate planurile.';
        $this->ps_versions_compliancy = ['min' => '8.0.0', 'max' => '9.99.99'];
    }

    public function install()
    {
        if (PHP_VERSION_ID < 80100 || !extension_loaded('bcmath') || !extension_loaded('curl')) { $this->_errors[] = 'fiscalm necesită PHP 8.1 sau mai nou, cu extensiile BCMath și cURL.'; return false; }
        return parent::install() && Db::getInstance()->execute('CREATE TABLE IF NOT EXISTS `' . _DB_PREFIX_ . 'fiscalm_job` (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY, shop INT NOT NULL, order_id INT NOT NULL,
            event_key VARCHAR(100) NOT NULL, operation VARCHAR(20) NOT NULL, payload MEDIUMTEXT NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT "pending", attempts INT NOT NULL DEFAULT 0,
            due DATETIME NOT NULL, remote_id VARCHAR(128) NOT NULL DEFAULT "", result MEDIUMTEXT NULL,
            UNIQUE KEY event (shop, event_key), KEY due (status, due)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4')
            && $this->installTab()
            && $this->registerHook(['actionOrderStatusPostUpdate', 'actionObjectOrderSlipAddAfter', 'actionAdminOrdersTrackingNumberUpdate', 'actionObjectOrderCarrierUpdateAfter', 'actionObjectSamedayAwbAddAfter', 'actionObjectSamedayAwbUpdateAfter', 'displayAdminOrder']);
    }

    private function installTab(): bool
    {
        $tab = new Tab();
        $tab->active = 0;
        $tab->class_name = 'AdminFiscalmPdf';
        $tab->module = $this->name;
        $tab->id_parent = -1;
        foreach (Language::getLanguages(false) as $language) { $tab->name[(int) $language['id_lang']] = 'fiscalm PDF'; }
        return $tab->add();
    }

    public function shopId(): int
    {
        return $this->activeShop ?? (int) $this->context->shop->id;
    }

    private function inOrderShop(Order $order, callable $callback): void
    {
        $previous = $this->activeShop;
        $this->activeShop = (int) $order->id_shop;
        try { $callback(); } finally { $this->activeShop = $previous; }
    }

    public function workerLock(): string
    {
        return 'ch-' . substr(hash('sha256', _DB_NAME_ . ':' . _DB_PREFIX_), 0, 32) . '-' . $this->shopId();
    }

    private static function json(array $data): string
    {
        return json_encode($data, JSON_HEX_TAG | JSON_HEX_AMP | JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
    }

    public function setting(string $name): string
    {
        return (string) Configuration::get('FISCALM_' . $name, null, null, $this->shopId());
    }

    public function client(): FiscalmClient
    {
        return new FiscalmClient($this->setting('URL'), $this->setting('TOKEN'), $this->setting('COMPANY'));
    }

    public function resolveJob(int $id, bool $checked): bool
    {
        if (!$checked) { return false; }
        Db::getInstance()->execute('UPDATE `' . _DB_PREFIX_ . 'fiscalm_job` SET status="resolved" WHERE id=' . $id . ' AND shop=' . $this->shopId() . ' AND status IN ("uncertain","blocked")');
        return Db::getInstance()->Affected_Rows() === 1;
    }

    public function getContent()
    {
        if (Shop::getContext() !== Shop::CONTEXT_SHOP) { return $this->displayError('Selectați un singur magazin.'); }
        $message = '';
        if (Tools::isSubmit('resolveFiscalm') && hash_equals(Tools::getAdminTokenLite('AdminModules'), (string) Tools::getValue('token'))) {
            if ($this->resolveJob((int) Tools::getValue('job'), Tools::getValue('checkedFiscalm') === '1')) {
                $message = $this->displayConfirmation('Verificarea în fiscalm a fost confirmată. Cererea nu va fi retrimisă.');
            }
        }
        if (Tools::isSubmit('saveFiscalm')) {
            if (!hash_equals(Tools::getAdminTokenLite('AdminModules'), (string) Tools::getValue('token'))) {
                return $this->displayError('Cerere invalidă. Reîncărcați pagina.');
            }
            $values = [];
            foreach (self::FIELDS as $field) {
                $values[$field] = trim((string) Tools::getValue('FISCALM_' . $field));
                if (in_array($field, ['TOKEN', 'SECRET'], true) && $values[$field] === '') { $values[$field] = $this->setting($field); }
            }
            try {
                new FiscalmClient($values['URL'], $values['TOKEN'], $values['COMPANY']);
                if (!preg_match('/^[A-Za-z0-9_-]{1,64}$/D', $values['CHANNEL']) || $values['COMPANY'] === ''
                    || strlen($values['SECRET']) < 32 || !ctype_digit($values['STATE'])
                    || ($values['AWB_FIELD'] !== '' && !preg_match('/^[a-z][a-z0-9_]{0,63}$/D', $values['AWB_FIELD']))) {
                    throw new DomainException('Verificați firma, canalul, starea comenzii și secretul webhook (cel puțin 32 de caractere).');
                }
                // Retrying a snapshot against a different company or channel could issue twice.
                if (Db::getInstance()->getValue('SELECT COUNT(*) FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE shop=' . $this->shopId()) > 0 && ($values['COMPANY'] !== $this->setting('COMPANY')
                    || $values['URL'] !== $this->setting('URL') || $values['CHANNEL'] !== $this->setting('CHANNEL'))) {
                    throw new DomainException('Destinația comenzilor este fixă după configurare. Folosiți un magazin separat pentru altă destinație.');
                }
                if ($this->setting('SINCE') === '') { Configuration::updateValue('FISCALM_SINCE', date('Y-m-d H:i:s'), false, null, $this->shopId()); }
                foreach ($values as $key => $value) { Configuration::updateValue('FISCALM_' . $key, $value, false, null, $this->shopId()); }
                $message = $this->displayConfirmation('Setările au fost salvate.');
            } catch (DomainException $error) { $message = $this->displayError($error->getMessage()); }
        }
        if (Tools::isSubmit('retryFiscalm') && hash_equals(Tools::getAdminTokenLite('AdminModules'), (string) Tools::getValue('token'))) {
            $invalid = Db::getInstance()->executeS('SELECT * FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE shop=' . $this->shopId() . ' AND operation="invalid" AND status="blocked"');
            foreach ($invalid ?: [] as $item) {
                try {
                    $payload = $this->mapOrder(new Order((int) $item['order_id']));
                    Db::getInstance()->execute('UPDATE `' . _DB_PREFIX_ . 'fiscalm_job` SET operation="order",payload="' . pSQL(self::json($payload)) . '" WHERE id=' . (int) $item['id']);
                } catch (DomainException $error) { continue; }
            }
            Db::getInstance()->execute('UPDATE `' . _DB_PREFIX_ . 'fiscalm_job` SET status="pending", attempts=0, due=NOW() WHERE shop=' . $this->shopId() . ' AND status="blocked"');
            $message = $this->displayConfirmation('Trimiterea cererilor blocate va fi reîncercată.');
        }
        $labels = ['URL' => 'Adresa fiscalm', 'TOKEN' => 'Cheie API (lăsați gol pentru a păstra cheia salvată)', 'COMPANY' => 'Cheia firmei în fiscalm',
            'CHANNEL' => 'ID-ul integrării de magazin', 'STATE' => 'Starea comenzii pentru facturare', 'SECRET' => 'Secret webhook (lăsați gol pentru a păstra secretul salvat)',
            'AWB_FIELD' => 'Câmp AWB din tabelul order_carrier (opțional)'];
        $html = $message . '<form method="post"><input type="hidden" name="token" value="' . self::escape(Tools::getAdminTokenLite('AdminModules')) . '">';
        foreach ($labels as $field => $label) {
            $secret = in_array($field, ['TOKEN', 'SECRET'], true);
            $html .= '<div class="form-group"><label for="ch-' . $field . '">' . $label . '</label>';
            if ($field === 'STATE') {
                $html .= '<select class="form-control" id="ch-STATE" name="FISCALM_STATE">';
                foreach (OrderState::getOrderStates((int) $this->context->language->id) as $state) {
                    $html .= '<option value="' . (int) $state['id_order_state'] . '"' . ((string) $state['id_order_state'] === $this->setting('STATE') ? ' selected' : '') . '>' . self::escape($state['name']) . '</option>';
                }
                $html .= '</select>';
            } else {
                $html .= '<input class="form-control" id="ch-' . $field . '" name="FISCALM_' . $field . '" type="' . ($secret ? 'password' : 'text') . '" autocomplete="new-password" value="' . ($secret ? '' : self::escape($this->setting($field))) . '">';
            }
            $html .= '</div>';
        }
        $html .= '<button class="btn btn-primary" name="saveFiscalm">Salvează setările</button> <button class="btn btn-default" name="retryFiscalm">Reîncearcă trimiterea</button></form>';
        $count = Db::getInstance()->getValue('SELECT COUNT(*) FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE shop=' . $this->shopId() . ' AND status NOT IN ("done","resolved")');
        $html .= '<p>Cereri de verificat sau netrimise: ' . (int) $count . '</p><p>Webhook: ' . self::escape($this->context->link->getModuleLink($this->name, 'webhook', [], true)) . '</p>';
        $uncertain = Db::getInstance()->executeS('SELECT id,order_id FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE shop=' . $this->shopId() . ' AND status IN ("uncertain","blocked")');
        foreach ($uncertain ?: [] as $item) {
            $html .= '<form method="post"><input type="hidden" name="token" value="' . self::escape(Tools::getAdminTokenLite('AdminModules')) . '">'
                . '<input type="hidden" name="job" value="' . (int) $item['id'] . '"><p>Cerere ' . (int) $item['id'] . ', comanda ' . (int) $item['order_id'] . '</p>'
                . '<label><input type="checkbox" name="checkedFiscalm" value="1" required> Rezultatul a fost verificat în fiscalm. Cererea poate fi închisă fără retrimitere.</label> '
                . '<button class="btn btn-default" name="resolveFiscalm">Confirmă verificarea în fiscalm</button></form>';
        }
        return $html;
    }

    public static function escape($value): string { return htmlspecialchars((string) $value, ENT_QUOTES, 'UTF-8'); }

    public function enqueue(int $orderId, string $key, string $operation, array $payload): void
    {
        Db::getInstance()->execute('INSERT IGNORE INTO `' . _DB_PREFIX_ . 'fiscalm_job` (shop,order_id,event_key,operation,payload,due) VALUES ('
            . $this->shopId() . ',' . $orderId . ',"' . pSQL($key) . '","' . pSQL($operation) . '","'
            . pSQL(self::json($payload)) . '",NOW())');
    }

    public function orderJob(int $id): array
    {
        return Db::getInstance()->getRow('SELECT * FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE shop=' . $this->shopId() . ' AND event_key="order:' . $id . '"') ?: [];
    }

    private function acceptsChild(int $id): bool
    {
        $parent = $this->orderJob($id);
        return $parent && (!empty($parent['remote_id']) || !in_array($parent['status'], ['resolved', 'done'], true));
    }

    public function hookActionOrderStatusPostUpdate(array $params): void
    {
        $id = (int) $params['id_order'];
        $order = new Order($id);
        $this->inOrderShop($order, function () use ($order, $id, $params): void {
            if ($this->setting('COMPANY') === '') { return; }
            if ((int) $params['newOrderStatus']->id === (int) $this->setting('STATE')) {
                try { $this->enqueue($id, 'order:' . $id, 'order', $this->mapOrder($order)); }
                catch (DomainException $error) { $this->enqueue($id, 'order:' . $id, 'invalid', ['reason' => $error->getMessage()]); }
            }
            if ((int) $params['newOrderStatus']->id === (int) Configuration::get('PS_OS_CANCELED') && $this->acceptsChild($id)) {
                $this->enqueue($id, 'cancel:' . $id, 'cancel', []);
            }
        });
    }

    public function mapOrder(Order $order): array
    {
        $address = new Address((int) $order->id_address_invoice);
        $country = new Country((int) $address->id_country);
        $state = new State((int) $address->id_state);
        $customer = new Customer((int) $order->id_customer);
        $currency = new Currency((int) $order->id_currency);
        if (!$state->name) { throw new DomainException('Completați județul adresei de facturare.'); }
        $lines = []; $rates = [];
        foreach ($order->getProducts() as $product) {
            $rate = FiscalmMapper::decimal($product['tax_rate']);
            $rates[$rate] = true;
            $lines[] = array_filter(['sku' => (string) $product['product_reference'], 'description' => $product['product_name'],
                'quantity' => (string) $product['product_quantity'], 'unit' => 'H87',
                'unit_price' => FiscalmMapper::decimal($product['unit_price_tax_incl']), 'vat_rate' => $rate], static fn($value) => $value !== '');
        }
        foreach (['shipping' => 'Transport', 'wrapping' => 'Ambalare'] as $field => $label) {
            $incl = FiscalmMapper::decimal($order->{'total_' . $field . '_tax_incl'});
            $excl = FiscalmMapper::decimal($order->{'total_' . $field . '_tax_excl'});
            if (bccomp($incl, '0', 6) > 0) {
                if (bccomp($excl, '0', 6) <= 0) { throw new DomainException('TVA pentru costurile comenzii este invalid. Verificați comanda.'); }
                if ($field === 'wrapping') { throw new DomainException('Cota TVA pentru ambalare necesită verificare.'); }
                $rate = FiscalmMapper::decimal($order->carrier_tax_rate);
                $expected = FiscalmMapper::round(bcmul($excl, bcadd('1', bcdiv($rate, '100', 8), 8), 8));
                $difference = bcsub($incl, $expected, 6);
                if (bccomp($difference, '0.01', 6) > 0 || bccomp($difference, '-0.01', 6) < 0) { throw new DomainException('Cota TVA pentru transport nu corespunde totalurilor.'); }
                $lines[] = ['description' => $label, 'quantity' => '1', 'unit' => 'H87', 'unit_price' => $incl, 'vat_rate' => $rate];
                $rates[$rate] = true;
            }
        }
        if (bccomp((string) $order->total_discounts_tax_incl, '0', 6) > 0) {
            // Multiple tax rates need the shop's exact cart-rule allocation, not a proportional guess.
            $normalized = array_unique(array_map(static fn($r) => bcadd((string) $r, '0', 2), array_keys($rates)));
            if (count($normalized) !== 1) { throw new DomainException('Reducerea cu mai multe cote TVA necesită verificare.'); }
            $lines[] = ['description' => 'Reducere', 'quantity' => '1', 'unit' => 'H87',
                'unit_price' => bcsub('0', FiscalmMapper::decimal($order->total_discounts_tax_incl), 6), 'vat_rate' => reset($normalized)];
        }
        $method = match ($order->module) { 'ps_wirepayment' => 'transfer', 'ps_cashondelivery' => 'cod', 'ps_checkpayment' => 'other', default => 'other' };
        return FiscalmMapper::map(['id' => $order->id, 'date' => $order->date_add, 'currency' => $currency->iso_code,
            'prices_include_vat' => true, 'total' => FiscalmMapper::decimal($order->total_paid_tax_incl), 'lines' => $lines,
            'buyer' => array_filter(['type' => $address->company ? 'company' : 'person',
                'name' => $address->company ?: trim($address->firstname . ' ' . $address->lastname),
                'cui' => $address->company ? $address->vat_number : '', 'email' => $customer->email,
                'address' => ['street' => trim($address->address1 . ' ' . $address->address2), 'city' => $address->city,
                    'county' => $country->iso_code === 'RO' ? 'RO-' . preg_replace('/^RO-/', '', strtoupper((string) $state->iso_code)) : $state->name, 'country' => $country->iso_code, 'postal' => $address->postcode]]),
            'payment' => ['method' => $method, 'processor' => $order->module, 'paid' => (bool) $order->hasBeenPaid()],
            'awb' => $this->awb($order)], $this->setting('CHANNEL'));
    }

    public function awb(Order $order): ?array
    {
        $field = $this->setting('AWB_FIELD') ?: 'tracking_number';
        if (!preg_match('/^[a-z][a-z0-9_]{0,63}$/D', $field)) { return null; }
        $rows = Db::getInstance()->executeS('SELECT * FROM `' . _DB_PREFIX_ . 'order_carrier` WHERE id_order=' . (int) $order->id);
        $numbers = [];
        foreach ($rows ?: [] as $row) { if (!empty($row[$field])) { $numbers[(string) $row[$field]] = true; } }
        $carrier = new Carrier((int) $order->id_carrier);
        // Official sameday-courier/prestashop-plugin SamedayAwb storage interface.
        if ($carrier->external_module_name === 'samedaycourier') {
            $table = _DB_PREFIX_ . 'sameday_awb';
            $exists = Db::getInstance()->getValue('SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name="' . pSQL($table) . '"');
            if ($exists) {
                $awbs = Db::getInstance()->executeS('SELECT awb_number FROM `' . bqSQL($table) . '` WHERE id_order=' . (int) $order->id);
                foreach ($awbs ?: [] as $awb) { if (!empty($awb['awb_number'])) { $numbers[(string) $awb['awb_number']] = true; } }
            }
        }
        if (count($numbers) !== 1) { return null; }
        return ['courier' => $carrier->name, 'number' => (string) array_key_first($numbers)];
    }

    public function hookActionAdminOrdersTrackingNumberUpdate(array $params): void
    {
        $this->queueAwb($params['order']);
    }

    public function hookActionObjectOrderCarrierUpdateAfter(array $params): void
    {
        $this->queueAwb(new Order((int) $params['object']->id_order));
    }

    public function hookActionObjectSamedayAwbAddAfter(array $params): void
    {
        $this->queueAwb(new Order((int) $params['object']->id_order));
    }

    public function hookActionObjectSamedayAwbUpdateAfter(array $params): void
    {
        $this->hookActionObjectSamedayAwbAddAfter($params);
    }

    private function queueAwb(Order $order): void
    {
        $this->inOrderShop($order, function () use ($order): void {
            if (!$this->acceptsChild((int) $order->id)) { return; }
            $awb = $this->awb($order);
            if ($awb) { $this->enqueue((int) $order->id, 'awb:' . $order->id . ':' . hash('sha256', self::json($awb)), 'awb', $awb); }
        });
    }

    public function hookActionObjectOrderSlipAddAfter(array $params): void
    {
        $slip = $params['object'];
        $order = new Order((int) $slip->id_order);
        $this->inOrderShop($order, function () use ($order, $slip): void {
            if (!$this->acceptsChild((int) $order->id)) { return; }
            $this->enqueue((int) $order->id, 'refund:' . (int) $slip->id, 'refund', ['slip_id' => (int) $slip->id]);
        });
    }

    private function refundPayload(array $job, array $parent): array
    {
        $receipt = json_decode($job['payload'], true);
        $slip = new OrderSlip((int) $receipt['slip_id']);
        if ((int) $slip->id_order !== (int) $job['order_id']) { throw new DomainException('Retur invalid.'); }
        $order = new Order((int) $job['order_id']);
        $original = json_decode($parent['payload'], true);
        if ($this->mapOrder($order)['lines'] !== $original['lines']) { throw new DomainException('Comanda a fost modificată. Verificați returul.'); }
        // Cart discounts or partial money-only returns cannot be represented as whole invoice positions.
        if (bccomp((string) $order->total_discounts_tax_incl, '0', 6) !== 0) { throw new DomainException('Verificați returul în fiscalm.'); }
        $positions = []; $quantities = []; $refunded = []; $position = 0;
        foreach ($order->getProducts() as $detail => $product) {
            $positions[$detail] = ++$position;
            $quantities[$detail] = (int) $product['product_quantity'];
        }
        foreach (OrderSlip::getOrdersSlipDetail((int) $slip->id) as $detail) {
            $id = (int) $detail['id_order_detail'];
            $product = new OrderDetail($id);
            if (bccomp((string) $detail['amount_tax_incl'], bcmul((string) $product->unit_price_tax_incl, (string) $detail['product_quantity'], 6), 2) !== 0) {
                throw new DomainException('Returul parțial necesită verificare în fiscalm.');
            }
            $refunded[$id] = (int) $detail['product_quantity'];
        }
        $body = FiscalmMapper::refund($positions, $quantities, $refunded);
        if (bccomp((string) $slip->total_shipping_tax_incl, '0', 6) > 0) {
            if (bccomp((string) $slip->total_shipping_tax_incl, (string) $order->total_shipping_tax_incl, 2) !== 0) {
                throw new DomainException('Returul transportului necesită verificare.');
            }
            $body['lines'][] = $position + 1;
        }
        return $body;
    }

    public function sweep(): void
    {
        if ($this->setting('SINCE') === '' || !ctype_digit($this->setting('STATE'))) { return; }
        $orders = Db::getInstance()->executeS('SELECT o.id_order FROM `' . _DB_PREFIX_ . 'orders` o WHERE o.id_shop=' . $this->shopId()
            . ' AND o.current_state=' . (int) $this->setting('STATE') . ' AND o.date_add>="' . pSQL($this->setting('SINCE')) . '"'
            . ' AND NOT EXISTS (SELECT 1 FROM `' . _DB_PREFIX_ . 'fiscalm_job` j WHERE j.shop=o.id_shop AND j.event_key=CONCAT("order:",o.id_order)) LIMIT 100');
        foreach ($orders ?: [] as $row) { $this->hookActionOrderStatusPostUpdate(['id_order' => (int) $row['id_order'], 'newOrderStatus' => new OrderState((int) $this->setting('STATE'))]); }
    }

    public function work(): void
    {
        $db = Db::getInstance(); $shop = $this->shopId();
        $lock = $this->workerLock();
        if (!(int) $db->getValue('SELECT GET_LOCK("' . $lock . '",0)')) { return; }
        try {
            // Closing an unsent parent must not leave children permanently occupying the queue.
            $reason = pSQL(self::json(['reason' => 'Comanda nu a fost trimisă în fiscalm. Cererea dependentă a fost închisă.']));
            $db->execute('UPDATE `' . _DB_PREFIX_ . 'fiscalm_job` child LEFT JOIN `' . _DB_PREFIX_ . 'fiscalm_job` parent'
                . ' ON parent.shop=child.shop AND parent.event_key=CONCAT("order:",child.order_id)'
                . ' SET child.status="resolved",child.result="' . $reason . '" WHERE child.shop=' . $shop
                . ' AND child.status="pending" AND child.operation IN ("awb","cancel","refund")'
                . ' AND (parent.id IS NULL OR (parent.remote_id="" AND parent.status IN ("done","resolved")))');
            $jobs = $db->executeS('SELECT j.* FROM `' . _DB_PREFIX_ . 'fiscalm_job` j WHERE j.shop=' . $shop . ' AND j.status="pending" AND j.due<=NOW()'
                . ' AND NOT EXISTS (SELECT 1 FROM `' . _DB_PREFIX_ . 'fiscalm_job` earlier WHERE earlier.shop=j.shop AND earlier.order_id=j.order_id AND earlier.id<j.id AND earlier.status NOT IN ("done","resolved")) ORDER BY j.id LIMIT 10');
            foreach ($jobs ?: [] as $job) {
                $attempts = (int) $job['attempts'] + 1;
                $operation = $job['operation']; $data = []; $status = 400; $sent = false;
                $reason = $operation === 'invalid' ? (json_decode($job['payload'], true)['reason'] ?? 'Verificați comanda înainte de trimitere.') : 'fiscalm nu a confirmat cererea. Verificați conexiunea și cheia API.';
                if ($operation !== 'invalid') {
                    $parent = $this->orderJob((int) $job['order_id']);
                    if ($operation !== 'order' && empty($parent['remote_id'])) {
                        $db->execute('UPDATE `' . _DB_PREFIX_ . 'fiscalm_job` SET status="blocked",result="' . pSQL(self::json(['reason' => 'Comanda nu a fost confirmată în fiscalm.'])) . '" WHERE id=' . (int) $job['id']);
                        continue;
                    }
                    $path = '/orders' . ($operation === 'order' ? '' : '/' . rawurlencode($parent['remote_id']) . '/' . $operation);
                    try {
                        $payload = $operation === 'refund' ? $this->refundPayload($job, $parent) : json_decode($job['payload'], true);
                        if (in_array($operation, ['refund', 'cancel'], true)) {
                            // Persist before sending. A crash or timeout may mean the credit note exists.
                            $db->execute('UPDATE `' . _DB_PREFIX_ . 'fiscalm_job` SET status="uncertain" WHERE id=' . (int) $job['id']);
                        }
                        $client = $this->client();
                        $sent = true;
                        $response = $client->request('POST', $path, $payload);
                        $status = $response['status']; $data = $response['data'];
                        if ($status >= 400 && $status < 500) { $reason = 'fiscalm a refuzat cererea. Verificați comanda și configurarea.'; }
                    } catch (DomainException $error) { $status = 400; $reason = $error->getMessage(); }
                }
                $success = $status >= 200 && $status < 300 && ($data['ok'] ?? false)
                    && ($operation !== 'order' || !empty($data['id']));
                $irreversible = in_array($operation, ['refund', 'cancel'], true);
                $retry = !$irreversible && ($status === 0 || $status === 429 || $status >= 500) && $attempts < 8;
                $db->execute('UPDATE `' . _DB_PREFIX_ . 'fiscalm_job` SET status="' . ($success ? 'done' : ($irreversible && $sent ? 'uncertain' : ($retry ? 'pending' : 'blocked')))
                    . '",attempts=' . $attempts . ',due=DATE_ADD(NOW(), INTERVAL ' . min(3600, 30 * 2 ** $attempts) . ' SECOND)'
                    . ($success ? ',remote_id="' . pSQL((string) ($data['id'] ?? '')) . '",result="' . pSQL(self::json(array_intersect_key($data, array_flip(['id', 'invoice_id', 'status', 'draft_id'])))) . '"' : '')
                    . (!$success ? ',result="' . pSQL(self::json(['reason' => $reason])) . '"' : '')
                    . ' WHERE id=' . (int) $job['id']);
            }
        } finally { $db->execute('SELECT RELEASE_LOCK("' . $lock . '")'); }
    }

    public function refreshOrder(int $id): bool
    {
        $db = Db::getInstance();
        $lock = $this->workerLock();
        if (!(int) $db->getValue('SELECT GET_LOCK("' . $lock . '",0)')) { return false; }
        try {
            $job = $this->orderJob($id);
            if (empty($job['remote_id'])) { return false; }
            $db->execute('UPDATE `' . _DB_PREFIX_ . 'fiscalm_job` SET due=DATE_ADD(NOW(), INTERVAL 5 MINUTE) WHERE id=' . (int) $job['id']);
            $response = $this->client()->request('GET', '/orders/' . rawurlencode($job['remote_id']));
            if ($response['status'] !== 200 || ($response['data']['id'] ?? '') !== $job['remote_id']) { return false; }
            $data = array_intersect_key($response['data'], array_flip(['id', 'invoice_id', 'invoice_number', 'status', 'spv_status', 'draft_id']));
            if (!isset($data['spv_status']) && is_array($response['data']['spv'] ?? null)) {
                $data['spv_status'] = $response['data']['spv']['status'] ?? '';
            }
            if (!empty($data['invoice_id']) && empty($data['invoice_number'])) {
                $invoice = $this->client()->request('GET', '/invoicing/issued/' . rawurlencode($data['invoice_id']));
                if ($invoice['status'] === 200 && isset($invoice['data']['series'], $invoice['data']['number'])) {
                    $data['invoice_number'] = $invoice['data']['series'] . '-' . $invoice['data']['number'];
                }
            }
            return $db->execute('UPDATE `' . _DB_PREFIX_ . 'fiscalm_job` SET result="' . pSQL(self::json($data)) . '",due=DATE_ADD(NOW(), INTERVAL 5 MINUTE) WHERE id=' . (int) $job['id']);
        } finally { $db->execute('SELECT RELEASE_LOCK("' . $lock . '")'); }
    }

    public function hookDisplayAdminOrder(array $params): string
    {
        $html = '';
        $this->inOrderShop(new Order((int) $params['id_order']), function () use ($params, &$html): void { $html = $this->renderOrder($params); });
        return $html;
    }

    private function renderOrder(array $params): string
    {
        $job = $this->orderJob((int) $params['id_order']);
        if (!$job) { return ''; }
        $data = json_decode($job['result'] ?? '{}', true) ?: [];
        $labels = ['draft' => 'Ciornă', 'issued' => 'Facturată', 'cancelled' => 'Anulată', 'storno' => 'Stornată',
            'pending' => 'În așteptare', 'blocked' => 'Necesită verificare', 'uncertain' => 'Rezultat de verificat în fiscalm', 'done' => 'Primită în fiscalm', 'resolved' => 'Cerere închisă după verificare'];
        $status = $data['status'] ?? $job['status'];
        $html = '<div class="card"><div class="card-header"><h3 class="card-header-title">fiscalm</h3></div><div class="card-body"><p>Stare: ' . self::escape($labels[$status] ?? 'Necesită verificare') . '</p>';
        if (!empty($data['invoice_id'])) {
            $html .= '<p>Factură: ' . self::escape($data['invoice_number'] ?? $data['invoice_id']) . '</p><a href="' . self::escape($this->context->link->getAdminLink('AdminFiscalmPdf') . '&setShopContext=s-' . $this->shopId() . '&id_order=' . (int) $job['order_id']) . '">Descarcă factura (PDF)</a>';
        }
        $html .= !empty($data['invoice_id']) ? '<p>SPV: ' . self::escape(['accepted' => 'Acceptată', 'rejected' => 'Respinsă', 'pending' => 'În așteptare'][$data['spv_status'] ?? 'pending'] ?? 'Necesită verificare') . '</p>' : '';
        $pending = Db::getInstance()->executeS('SELECT * FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE shop=' . $this->shopId() . ' AND order_id=' . (int) $params['id_order'] . ' AND status NOT IN ("done","resolved")');
        foreach ($pending ?: [] as $item) {
            $result = json_decode($item['result'] ?? '{}', true) ?: [];
            $html .= '<p>Cerere ' . (int) $item['id'] . ': ' . self::escape($labels[$item['status']] ?? 'Necesită verificare') . ' ' . self::escape($result['reason'] ?? '') . '</p>';
        }
        return $html . '<a href="' . self::escape($this->context->link->getAdminLink('AdminModules') . '&configure=fiscalm') . '">Verifică cererile fiscalm</a></div></div>';
    }
}
