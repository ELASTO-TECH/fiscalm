<?php
// Run in the real installed PrestaShop context from scenario.php.
$db->execute('UPDATE `' . _DB_PREFIX_ . 'fiscalm_job` SET status="done" WHERE event_key="invalid-test"');
$payload = json_decode($module->orderJob($id)['payload'], true);
$payload['external_id'] = (string) ($id + 100000);
$payload['lines'][0]['description'] = 'Cablu <2m & <3';
$module->enqueue($id + 100000, 'order:' . ($id + 100000), 'order', $payload);
$module->work();
$textJob = $module->orderJob($id + 100000);
same('done', $textJob['status']);
$remote = $module->client()->request('GET', '/orders/' . $textJob['remote_id']);
same('Cablu <2m & <3', $remote['data']['body']['lines'][0]['description']);

// Rounded shipping amounts must not manufacture a 21.04% tax rate.
$order = new Order($id);
$order->total_shipping_tax_incl = '15.99'; $order->total_shipping_tax_excl = '13.21';
$order->carrier_tax_rate = '21';
$order->total_paid_tax_incl = bcadd((string) $order->total_paid_tax_incl, '15.99', 6);
$shipping = $module->mapOrder($order);
same('21', end($shipping['lines'])['vat_rate']);
$order->total_shipping_tax_excl = '10';
refuses(fn() => $module->mapOrder($order));
$order = new Order($id);

Cache::clean('*');
// The actual ObjectModel carrier update hook must enqueue, with no HTTP call in the hook.
$carrierId = (int) $db->getValue('SELECT id_order_carrier FROM `' . _DB_PREFIX_ . 'order_carrier` WHERE id_order=' . $id);
$carrier = new OrderCarrier($carrierId);
if (!$carrierId) {
    $carrier->id_order = $id; $carrier->id_carrier = $order->id_carrier; $carrier->id_order_invoice = 0;
    $carrier->weight = '1'; $carrier->shipping_cost_tax_excl = '0'; $carrier->shipping_cost_tax_incl = '0';
    if (!$carrier->add()) { throw new RuntimeException('Carrier fixture failed'); }
}
$carrier->tracking_number = 'AWBEXEMPLU12';
if (!$carrier->update()) { throw new RuntimeException('Carrier tracking update failed'); }
$awbJob = $db->getRow('SELECT * FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE operation="awb" AND order_id=' . $id);
same('pending', $awbJob['status']);
$module->work();
same('done', $db->getValue('SELECT status FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE id=' . (int) $awbJob['id']));

// A cancellation may have committed before its 503. It must never retry.
$module->enqueue($id, 'cancel:' . $id, 'cancel', []);
$module->work();
$cancel = $db->getRow('SELECT * FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE operation="cancel" AND order_id=' . $id);
same('uncertain', $cancel['status']);
$module->work();
$remote = $module->client()->request('GET', '/orders/' . $module->orderJob($id)['remote_id']);
same(1, $remote['data']['cancel_calls']);
same(false, $module->resolveJob((int) $cancel['id'], false));
$_POST = ['resolveFiscalm' => '1', 'job' => (string) $cancel['id'], 'checkedFiscalm' => '1', 'token' => 'invalid'];
$module->getContent(); $_POST = [];
same('uncertain', $db->getValue('SELECT status FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE id=' . (int) $cancel['id']));
if (!str_contains($module->hookDisplayAdminOrder(['id_order' => $id]), 'de verificat')) { throw new RuntimeException('Uncertain operation hidden'); }
same(true, $module->resolveJob((int) $cancel['id'], true));
same(false, $module->resolveJob((int) $cancel['id'], true));

// Reject a missing/invalid refund receipt locally without claiming it was sent.
$module->enqueue($id, 'refund:missing', 'refund', ['slip_id' => 999999]);
$module->work();
$refund = $db->getRow('SELECT * FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE event_key="refund:missing"');
same('blocked', $refund['status']);

// A foreign connection reserves the real worker's lock: work() must leave a pending job untouched.
$payload['external_id'] = (string) ($id + 100001);
$module->enqueue($id + 100001, 'order:' . ($id + 100001), 'order', $payload);
$other->query('SELECT GET_LOCK("' . $module->workerLock() . '",0)');
$module->work();
same('pending', $module->orderJob($id + 100001)['status']);
$other->query('SELECT RELEASE_LOCK("' . $module->workerLock() . '")');
$module->work();
same('done', $module->orderJob($id + 100001)['status']);

// Back-office all-shops context must use the order's shop settings, not Context's default shop.
$shopRow = $db->getRow('SELECT * FROM `' . _DB_PREFIX_ . 'shop` WHERE id_shop=1');
unset($shopRow['id_shop']); $shopRow['name'] = 'Magazin exemplu secundar';
$db->insert('shop', $shopRow); $secondShop = (int) $db->Insert_ID();
foreach (['URL', 'TOKEN', 'COMPANY', 'CHANNEL', 'STATE', 'SECRET'] as $key) {
    Configuration::updateValue('FISCALM_' . $key, $module->setting($key), false, null, $secondShop);
}
$order = new Order($id); $order->id_shop = $secondShop; $order->update(); Cache::clean('*');
$module->hookActionOrderStatusPostUpdate(['id_order' => $id, 'newOrderStatus' => new OrderState(2)]);
$foreign = $db->getRow('SELECT * FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE shop=' . $secondShop . ' AND event_key="order:' . $id . '"');
same('pending', $foreign['status']);
$order->id_shop = 1; $order->update(); Cache::clean('*');
echo "Review regressions: text preservation, shipping VAT, real AWB hook, uncertain cancellation, local refund refusal, worker lock, multistore passed\n";

$payload['external_id'] = 'retry-once';
$module->enqueue($id + 100002, 'order:' . ($id + 100002), 'order', $payload);
$module->work();
$retry = $module->orderJob($id + 100002);
same('pending', $retry['status']); same(1, (int) $retry['attempts']);
$module->work(); same(1, (int) $module->orderJob($id + 100002)['attempts']);
$db->execute('UPDATE `' . _DB_PREFIX_ . 'fiscalm_job` SET due=NOW() WHERE id=' . (int) $retry['id']);
$module->work(); same('done', $module->orderJob($id + 100002)['status']);
echo "HTTP retry backoff and CSRF refusal passed\n";

// A complete original product position uses a real persisted PrestaShop credit slip.
same(true, $module->resolveJob((int) $refund['id'], true));
$slip = new OrderSlip();
$slip->id_customer = $order->id_customer; $slip->id_order = $id; $slip->conversion_rate = '1';
$slip->total_products_tax_excl = '0'; $slip->total_products_tax_incl = '0';
$slip->total_shipping_tax_excl = '0'; $slip->total_shipping_tax_incl = '0';
$slip->partial = 0; $slip->amount = '0';
if (!$slip->add()) { throw new RuntimeException('Credit slip fixture failed'); }
$first = reset($products);
$db->insert('order_slip_detail', ['id_order_slip' => $slip->id, 'id_order_detail' => $first['id_order_detail'],
    'product_quantity' => $first['product_quantity'],
    'amount_tax_excl' => bcmul((string) $first['unit_price_tax_excl'], (string) $first['product_quantity'], 6),
    'amount_tax_incl' => bcmul((string) $first['unit_price_tax_incl'], (string) $first['product_quantity'], 6)]);
$module->work();
$fullRefund = $db->getRow('SELECT * FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE event_key="refund:' . (int) $slip->id . '"');
same('done', $fullRefund['status']);
$remote = $module->client()->request('GET', '/orders/' . $module->orderJob($id)['remote_id']);
same(1, $remote['data']['refund_calls']);
echo "Real credit-slip hook and whole-line refund passed\n";

// The official Sameday storage adapter is scoped to the order's carrier and refuses ambiguity.
$db->execute('CREATE TABLE `' . _DB_PREFIX_ . 'sameday_awb` (id_order INT NOT NULL, awb_number VARCHAR(100) NOT NULL)');
$carrier->tracking_number = ''; $carrier->update();
$db->execute('UPDATE `' . _DB_PREFIX_ . 'carrier` SET external_module_name="samedaycourier" WHERE id_carrier=' . (int) $order->id_carrier);
$db->insert('sameday_awb', ['id_order' => $id, 'awb_number' => 'SAMEDAYEXEMPLU14']); Cache::clean('*');
same('SAMEDAYEXEMPLU14', $module->awb(new Order($id))['number']);
Hook::exec('actionObjectSamedayAwbAddAfter', ['object' => (object) ['id_order' => $id]]);
$sameday = $db->getRow('SELECT * FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE operation="awb" AND order_id=' . $id . ' ORDER BY id DESC');
same('pending', $sameday['status']);
$module->work();
$db->insert('sameday_awb', ['id_order' => $id, 'awb_number' => 'SAMEDAYCONFLICT15']);
same(null, $module->awb(new Order($id)));
echo "Sameday adapter and ambiguous AWB refusal passed\n";

// Ten orphaned children must not occupy all worker slots after their parents are closed.
for ($n = 0; $n < 10; $n++) {
    $orphan = $id + 200 + $n;
    $module->enqueue($orphan, 'order:' . $orphan, 'invalid', []);
    $parent = $module->orderJob($orphan);
    $db->execute('UPDATE `' . _DB_PREFIX_ . 'fiscalm_job` SET status="resolved" WHERE id=' . (int) $parent['id']);
    $module->enqueue($orphan, 'cancel:' . $orphan, 'cancel', []);
}
$payload['external_id'] = 'after-orphans';
$module->enqueue($id + 400, 'order:' . ($id + 400), 'order', $payload);
$module->work();
same('done', $module->orderJob($id + 400)['status']);
same('0', (string) $db->getValue('SELECT COUNT(*) FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE operation="cancel" AND status="pending"'));
// Current hooks also refuse to create a child of a closed, never-sent parent.
$db->execute('UPDATE `' . _DB_PREFIX_ . 'fiscalm_job` SET status="resolved",remote_id="" WHERE event_key="order:' . $id . '" AND shop=1');
$db->execute('DELETE FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE shop=1 AND event_key="cancel:' . $id . '"');
$count = (int) $db->getValue('SELECT COUNT(*) FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE shop=1');
$module->hookActionOrderStatusPostUpdate(['id_order' => $id, 'newOrderStatus' => new OrderState((int) Configuration::get('PS_OS_CANCELED'))]);
same($count, (int) $db->getValue('SELECT COUNT(*) FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE shop=1'));
echo "Resolved-parent orphan cleanup and actual hook refusal passed\n";

$db->execute('UPDATE `' . _DB_PREFIX_ . 'fiscalm_job` SET remote_id="missing-order",due=DATE_SUB(NOW(), INTERVAL 1 MINUTE) WHERE id=' . (int) $textJob['id']);
same(false, $module->refreshOrder($id + 100000));
same('1', (string) $db->getValue('SELECT due>NOW() FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE id=' . (int) $textJob['id']));
echo "Failed invoice refresh advances its retry slot\n";
