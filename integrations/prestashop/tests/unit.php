<?php
require_once file_exists(__DIR__ . '/../fiscalm/classes/Mapper.php') ? __DIR__ . '/../fiscalm/classes/Mapper.php' : '/var/www/html/modules/fiscalm/classes/Mapper.php';
function same($expected, $actual): void { if ($expected !== $actual) { throw new RuntimeException(var_export([$expected, $actual], true)); } }
function refuses(callable $fn): void { try { $fn(); } catch (DomainException $e) { return; } throw new RuntimeException('Expected refusal'); }
$order = ['id' => 12, 'date' => '2026-11-02 12:00:00', 'currency' => 'RON', 'prices_include_vat' => true,
    'total' => '229.90', 'buyer' => ['type' => 'person', 'name' => 'Client exemplu', 'address' => ['street' => 'Strada Exemplu 1', 'city' => 'Cluj', 'county' => 'Cluj', 'country' => 'RO']],
    'lines' => [['description' => 'Produs', 'quantity' => '2', 'unit' => 'H87', 'unit_price' => '121', 'vat_rate' => '21'],
        ['description' => 'Transport', 'quantity' => '1', 'unit' => 'H87', 'unit_price' => '12.10', 'vat_rate' => '21'],
        ['description' => 'Reducere', 'quantity' => '1', 'unit' => 'H87', 'unit_price' => '-24.20', 'vat_rate' => '21']],
    'payment' => ['method' => 'cod', 'paid' => false]];
same('12', FiscalmMapper::map($order, 'prestashop')['external_id']);
same(false, FiscalmMapper::map($order, 'prestashop')['payment']['paid']);
$net = $order; $net['prices_include_vat'] = false;
$net['lines'][0]['unit_price'] = '100'; $net['lines'][1]['unit_price'] = '10'; $net['lines'][2]['unit_price'] = '-20';
same('100', FiscalmMapper::map($net, 'prestashop')['lines'][0]['unit_price']);
$bad = $order; $bad['total'] = '229.89'; refuses(fn() => FiscalmMapper::map($bad, 'prestashop'));
$company = $order; $company['buyer']['type'] = 'company'; refuses(fn() => FiscalmMapper::map($company, 'prestashop'));
$company['buyer']['cui'] = 'TEST-CUI'; same('company', FiscalmMapper::map($company, 'prestashop')['buyer']['type']);
same('1.01', FiscalmMapper::round('1.005')); same('-1.01', FiscalmMapper::round('-1.005'));
same(['lines' => [1, 2]], FiscalmMapper::refund([10 => 1, 11 => 2], [10 => 2, 11 => 1], [10 => 2, 11 => 1]));
same(['lines' => [2]], FiscalmMapper::refund([11 => 2], [11 => 1], [11 => 1]));
refuses(fn() => FiscalmMapper::refund([10 => 1], [10 => 2], [10 => 1]));
refuses(fn() => FiscalmMapper::refund([], [], [10 => 1]));
$secret = str_repeat('s', 32); $body = '{}'; $signature = 'sha256=' . hash_hmac('sha256', '1000.' . $body, $secret);
same(true, FiscalmMapper::verifyWebhook($secret, '1000', $body, $signature, 1000));
same(false, FiscalmMapper::verifyWebhook($secret, '1000', $body, $signature, 1301));
same(false, FiscalmMapper::verifyWebhook($secret, '1000', '{"changed":true}', $signature, 1000));
echo "Mapping, rounding, refunds and signature tests passed\n";
