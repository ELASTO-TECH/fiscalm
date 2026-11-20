<?php
require '/var/www/html/config/config.inc.php';
global $kernel;
$kernel = class_exists('AdminKernel') ? new AdminKernel('prod', false) : new AppKernel('prod', false);
$kernel->boot();
require __DIR__ . '/unit.php';
$module = Module::getInstanceByName('fiscalm');
if (!$module->isInstalled('fiscalm') && !$module->install()) { throw new RuntimeException('Module install failed'); }
$context = Context::getContext();
Shop::setContext(Shop::CONTEXT_SHOP, 1);
$context->shop = new Shop(1);
$context->currency = new Currency((int) Configuration::get('PS_CURRENCY_DEFAULT'));
$context->language = new Language((int) Configuration::get('PS_LANG_DEFAULT')); 
foreach (['URL' => 'http://mock:8080', 'TOKEN' => getenv('E2E_TOKEN'), 'COMPANY' => 'NORD', 'CHANNEL' => 'prestashop', 'STATE' => '2', 'SECRET' => bin2hex(random_bytes(32))] as $key => $value) {
    Configuration::updateValue('FISCALM_' . $key, $value, false, null, 1);
}
// Use PrestaShop's installed synthetic order, normalizing totals to one tax rate.
$id = (int) Db::getInstance()->getValue('SELECT id_order FROM ' . _DB_PREFIX_ . 'orders ORDER BY id_order');
if (!$id) { throw new RuntimeException('No sample order installed'); }
$order = new Order($id);
$products = $order->getProducts();
$total = '0';
foreach ($products as $product) {
    $total = bcadd($total, bcmul((string) $product['product_quantity'], (string) $product['unit_price_tax_incl'], 6), 6);
}
foreach (['total_shipping_tax_incl', 'total_shipping_tax_excl', 'total_wrapping_tax_incl', 'total_wrapping_tax_excl', 'total_discounts_tax_incl', 'total_discounts_tax_excl'] as $key) { $order->$key = '0'; }
$order->total_paid_tax_incl = FiscalmMapper::round($total);
$order->update();
$address = new Address((int) $order->id_address_invoice); $address->company = ''; $address->update();
same(true, is_bool($module->mapOrder(new Order($id))['payment']['paid']));
$module->hookActionOrderStatusPostUpdate(['id_order' => $id, 'newOrderStatus' => new OrderState(2)]);
$job = $module->orderJob($id);
if ($job['status'] !== 'pending') { throw new RuntimeException('Hook performed network I/O'); }
$module->work();
$job = $module->orderJob($id);
if ($job['status'] !== 'done' || !$job['remote_id']) { throw new RuntimeException('Order submission failed: ' . $job['status']); }
$module->hookActionOrderStatusPostUpdate(['id_order' => $id, 'newOrderStatus' => new OrderState(2)]);
if ((int) Db::getInstance()->getValue('SELECT COUNT(*) FROM ' . _DB_PREFIX_ . 'fiscalm_job') !== 1) { throw new RuntimeException('Duplicate job'); }
$state = $module->client()->request('GET', '/orders/' . $job['remote_id']);
if ($state['status'] !== 200 || $state['data']['status'] !== 'draft') { throw new RuntimeException('State fetch failed'); }
if (!$module->refreshOrder($id)) { throw new RuntimeException('Refresh failed'); }
echo 'PrestaShop ' . _PS_VERSION_ . ": installed module, real order hook, mapping, queue, HTTP intake and duplicate trigger passed\n";

$context->employee = new Employee(1);
$html = $module->getContent();
if (str_contains($html, getenv('E2E_TOKEN')) || str_contains($html, $module->setting('SECRET'))) { throw new RuntimeException('Secret rendered'); }
$module->enqueue($id, 'invalid-test', 'invalid', []);
$module->work();
if (Db::getInstance()->getValue('SELECT status FROM ' . _DB_PREFIX_ . 'fiscalm_job WHERE event_key="invalid-test"') !== 'blocked') { throw new RuntimeException('Invalid mapping not held'); }
$db = Db::getInstance();
$workerLock = $module->workerLock();
if (!(int) $db->getValue('SELECT GET_LOCK("' . $workerLock . '",0)')) { throw new RuntimeException('Lock unavailable'); }
// A second independent connection must not acquire the worker reservation.
$parameters = require '/var/www/html/app/config/parameters.php';
$config = $parameters['parameters'];
$other = new PDO('mysql:host=' . $config['database_host'] . ';dbname=' . $config['database_name'], $config['database_user'], $config['database_password']);
if ((int) $other->query('SELECT GET_LOCK("' . $workerLock . '",0)')->fetchColumn() !== 0) { throw new RuntimeException('Concurrent worker reservation'); }
$db->execute('SELECT RELEASE_LOCK("' . $workerLock . '")');
echo "Secret fields, refusal queue and two-connection worker reservation passed\n";

function webhookStatus(string $body, string $timestamp, string $signature): int {
    $ch = curl_init('http://localhost/index.php?fc=module&module=fiscalm&controller=webhook');
    curl_setopt_array($ch, [CURLOPT_POST => true, CURLOPT_POSTFIELDS => $body, CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER => ['X-Fiscalm-Event-Id: ' . (json_decode($body, true)['id'] ?? ''), 'Host: ' . Configuration::get('PS_SHOP_DOMAIN'), 'Content-Type: application/json', 'X-Fiscalm-Timestamp: ' . $timestamp, 'X-Fiscalm-Signature: ' . $signature]]);
    curl_exec($ch); $status = (int) curl_getinfo($ch, CURLINFO_RESPONSE_CODE); curl_close($ch); return $status;
}
$event = json_encode(['id' => 'event-example-1', 'type' => 'invoice.issued', 'order_id' => $job['remote_id'], 'company' => 'NORD', 'channel' => 'prestashop', 'external_id' => (string) $id]);
$timestamp = (string) time();
$signature = 'sha256=' . hash_hmac('sha256', $timestamp . '.' . $event, $module->setting('SECRET'));
$signedStatus = webhookStatus($event, $timestamp, $signature);
if ($signedStatus !== 200) { throw new RuntimeException('Signed webhook failed: HTTP ' . $signedStatus); }
if (webhookStatus($event, $timestamp, $signature) !== 200) { throw new RuntimeException('Duplicate webhook not acknowledged'); }
if (webhookStatus($event . ' ', $timestamp, $signature) !== 401) { throw new RuntimeException('Tampered webhook accepted'); }
$old = (string) (time() - 301);
if (webhookStatus($event, $old, 'sha256=' . hash_hmac('sha256', $old . '.' . $event, $module->setting('SECRET'))) !== 401) { throw new RuntimeException('Stale webhook accepted'); }
echo "Real HTTP webhook: signed accepted, tampered and stale refused\n";

require __DIR__ . '/regressions.php';
