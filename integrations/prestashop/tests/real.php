<?php
require __DIR__ . '/real-bootstrap.php';
$module->hookActionOrderStatusPostUpdate(['id_order' => $id, 'newOrderStatus' => new OrderState(2)]);
$module->work(); $job = $module->orderJob($id);
if ($job['status'] !== 'done') {
    $error = $module->client()->request('POST', '/orders', json_decode($job['payload'], true));
    throw new RuntimeException('Real fiscalm intake refused: ' . ($error['data']['error']['code'] ?? 'network') . ': ' . ($error['data']['error']['message'] ?? ''));
}
$remote = $module->client()->request('GET', '/orders/' . $job['remote_id']);
if ($remote['status'] !== 200 || $remote['data']['status'] !== 'draft' || $remote['data']['invoice_id'] !== null) { throw new RuntimeException('Shadow mode issued or lost the draft'); }
$draft = $module->client()->request('GET', '/invoicing/drafts/' . $remote['data']['draft_id']);
if ($draft['status'] !== 200) { throw new RuntimeException('Real draft missing'); }
$replay = $module->client()->request('POST', '/orders', json_decode($job['payload'], true));
if ($replay['status'] !== 200 || $replay['data']['id'] !== $job['remote_id']) { throw new RuntimeException('Real idempotent replay failed'); }
$issue = $module->client()->request('POST', '/invoicing/issue', ['id' => $remote['data']['draft_id'], 'version' => $remote['data']['draft_version']]);
if ($issue['status'] !== 403) { throw new RuntimeException('Shadow issue not refused'); }
$module->enqueue($id, 'real-awb', 'awb', ['courier' => 'Curier exemplu', 'number' => 'AWBEXEMPLU13']); $module->work();
$awb = $module->client()->request('GET', '/orders/' . $job['remote_id']);
if (($awb['data']['awb']['number'] ?? '') !== 'AWBEXEMPLU13') { throw new RuntimeException('Real AWB failed'); }
$cancelBody = json_decode($job['payload'], true); $cancelBody['external_id'] = 'cancel-example';
$otherOrder = $module->client()->request('POST', '/orders', $cancelBody);
$cancel = $module->client()->request('POST', '/orders/' . $otherOrder['data']['id'] . '/cancel', []);
if (($cancel['data']['status'] ?? '') !== 'cancelled') { throw new RuntimeException('Real draft cancellation failed'); }
file_put_contents('/tmp/real-order.json', json_encode(['id' => $job['remote_id']]));
echo "Real fiscalm: mapped PrestaShop order, persisted draft, idempotent replay, shadow issue refusal, AWB and cancellation passed\n";
