<?php
require '/var/www/html/config/config.inc.php';
global $kernel;
$kernel = class_exists('AdminKernel') ? new AdminKernel('prod', false) : new AppKernel('prod', false);
$kernel->boot();
$context = Context::getContext(); $context->shop = new Shop(1); Shop::setContext(Shop::CONTEXT_SHOP, 1);
$context->language = new Language((int) Configuration::get('PS_LANG_DEFAULT'));
$context->currency = new Currency((int) Configuration::get('PS_CURRENCY_DEFAULT'));
$module = Module::getInstanceByName('fiscalm'); $db = Db::getInstance();
$credential = json_decode(file_get_contents('/tmp/real-commerce.json'), true);
// A fresh destination after the mock phase, entirely within the disposable test database.
$db->execute('DELETE FROM `' . _DB_PREFIX_ . 'fiscalm_job`');
foreach (['URL' => 'http://fiscalm:8790', 'TOKEN' => $credential['token'], 'CHANNEL' => $credential['channel']] as $key => $value) {
    Configuration::updateValue('FISCALM_' . $key, $value, false, null, 1);
}
$id = (int) $db->getValue('SELECT id_order FROM `' . _DB_PREFIX_ . 'orders` ORDER BY id_order');
$order = new Order($id);
$address = new Address((int) $order->id_address_invoice);
$address->id_country = Country::getByIso('RO');
$address->id_state = (int) $db->getValue('SELECT id_state FROM `' . _DB_PREFIX_ . 'state` WHERE id_country=' . (int) $address->id_country . ' AND iso_code="CJ"');
$address->firstname = 'Client'; $address->lastname = 'Exemplu'; $address->company = ''; $address->vat_number = '';
$address->address1 = 'Strada Exemplu 1'; $address->address2 = ''; $address->city = 'Cluj-Napoca'; $address->postcode = '000000';
if (!$address->id_state || !$address->update()) { throw new RuntimeException('Romanian address fixture failed'); }
$currency = (int) Currency::getIdByIsoCode('RON');
if (!$currency) { throw new RuntimeException('RON currency missing'); }
$order->id_currency = $currency; $order->conversion_rate = '1'; $order->date_add = date('Y-m-d H:i:s');
$tax = (int) $db->getValue('SELECT id_tax FROM `' . _DB_PREFIX_ . 'tax` WHERE rate=21 AND active=1');
if (!$tax) { throw new RuntimeException('21% tax fixture missing'); }
$quantity = 0;
foreach ($order->getProducts() as $product) {
    $detail = (int) $product['id_order_detail']; $qty = (int) $product['product_quantity']; $quantity += $qty;
    $db->execute('UPDATE `' . _DB_PREFIX_ . 'order_detail` SET product_name="Produs exemplu",unit_price_tax_excl=100,unit_price_tax_incl=121,total_price_tax_excl=' . (100 * $qty) . ',total_price_tax_incl=' . (121 * $qty) . ',tax_rate=21 WHERE id_order_detail=' . $detail);
    $db->execute('DELETE FROM `' . _DB_PREFIX_ . 'order_detail_tax` WHERE id_order_detail=' . $detail);
    $db->insert('order_detail_tax', ['id_order_detail' => $detail, 'id_tax' => $tax, 'unit_amount' => '21', 'total_amount' => (string) (21 * $qty)]);
}
$order->total_paid_tax_incl = (string) (121 * $quantity); $order->total_paid_tax_excl = (string) (100 * $quantity);
$order->total_products = (string) (100 * $quantity); $order->total_products_wt = (string) (121 * $quantity); $order->total_paid = $order->total_paid_tax_incl;
$order->update(); Cache::clean('*');
