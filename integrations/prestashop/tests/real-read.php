<?php
require __DIR__ . '/real-bootstrap.php';
if (!$module->refreshOrder($id)) { throw new RuntimeException('Issued-state refresh failed'); }
$job = $module->orderJob($id); $result = json_decode($job['result'], true);
if (empty($result['invoice_id']) || empty($result['invoice_number'])) { throw new RuntimeException('Invoice identity not stored'); }
$pdf = $module->client()->request('GET', '/invoicing/issued/' . rawurlencode($result['invoice_id']) . '/pdf', null, true);
if ($pdf['status'] !== 200 || !str_starts_with($pdf['data'], '%PDF-')) { throw new RuntimeException('Real invoice PDF failed'); }
$context->employee = new Employee(1);
$panel = $module->hookDisplayAdminOrder(['id_order' => $id]);
if (!str_contains($panel, $result['invoice_number']) || !str_contains($panel, 'Descarcă factura (PDF)')
    || str_contains($panel, $module->setting('TOKEN'))) { throw new RuntimeException('Invoice panel disclosure or missing PDF action'); }
$order = new Order($id); $products = $order->getProducts(); $first = reset($products);
$slip = new OrderSlip(); $slip->id_customer = $order->id_customer; $slip->id_order = $id; $slip->conversion_rate = '1';
$slip->total_products_tax_excl = bcmul((string) $first['unit_price_tax_excl'], (string) $first['product_quantity'], 6);
$slip->total_products_tax_incl = bcmul((string) $first['unit_price_tax_incl'], (string) $first['product_quantity'], 6);
$slip->total_shipping_tax_excl = '0'; $slip->total_shipping_tax_incl = '0'; $slip->partial = 0; $slip->amount = $slip->total_products_tax_incl;
Cache::clean('*');
if (!$slip->add()) { throw new RuntimeException('Real refund receipt failed'); }
$db->insert('order_slip_detail', ['id_order_slip' => $slip->id, 'id_order_detail' => $first['id_order_detail'],
    'product_quantity' => $first['product_quantity'], 'amount_tax_excl' => $slip->total_products_tax_excl, 'amount_tax_incl' => $slip->total_products_tax_incl]);
$module->work();
$refund = $db->getRow('SELECT * FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE event_key="refund:' . (int) $slip->id . '"');
if ($refund['status'] !== 'done') { throw new RuntimeException('Real refund draft refused'); }
$credit = json_decode($refund['result'], true);
$draft = $module->client()->request('GET', '/invoicing/drafts/' . $credit['draft_id']);
if ($draft['status'] !== 200 || $draft['data']['totals']['total'] !== '-' . FiscalmMapper::round((string) $slip->total_products_tax_incl)) {
    throw new RuntimeException('Refund draft amount or unnumbered status incorrect');
}
$issuedCredit = $module->client()->request('GET', '/invoicing/issued/' . $credit['draft_id']);
if ($issuedCredit['status'] !== 404) { throw new RuntimeException('Refund draft exposed as an issued invoice'); }
echo "Synthetic historical invoice: real PDF bytes, invoice number/panel, and exact unnumbered whole-line refund draft passed\n";
