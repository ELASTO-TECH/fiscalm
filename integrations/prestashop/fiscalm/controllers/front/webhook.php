<?php
/** SPDX-License-Identifier: AFL-3.0
 * Copyright 2026 fiscalm contributors. Licensed under the Academic Free License version 3.0. */
class FiscalmWebhookModuleFrontController extends ModuleFrontController
{
    public $ssl = true;
    public function postProcess()
    {
        $body = file_get_contents('php://input', false, null, 0, 65537);
        $timestamp = $_SERVER['HTTP_X_FISCALM_TIMESTAMP'] ?? '';
        $signature = $_SERVER['HTTP_X_FISCALM_SIGNATURE'] ?? '';
        if ($_SERVER['REQUEST_METHOD'] !== 'POST' || strlen($body) > 65536
            || !FiscalmMapper::verifyWebhook($this->module->setting('SECRET'), $timestamp, $body, $signature, time())) {
            http_response_code(401); exit;
        }
        $event = json_decode($body, true);
        if (!is_array($event) || !in_array($event['type'] ?? '', ['invoice.issued', 'invoice.spv_accepted',
            'invoice.spv_rejected', 'invoice.collected', 'order.refused'], true)
            || ($event['channel'] ?? '') !== $this->module->setting('CHANNEL')
            || ($event['company'] ?? '') !== $this->module->setting('COMPANY')
            || !ctype_digit((string) ($event['external_id'] ?? ''))
            || !is_string($event['id'] ?? null) || !preg_match('/^[A-Za-z0-9_-]{1,64}$/D', $event['id'])
            || ($_SERVER['HTTP_X_FISCALM_EVENT_ID'] ?? '') !== $event['id']) { http_response_code(400); exit; }
        // Signed bodies are notifications only. Read authoritative state through the fixed API destination.
        $id = (int) $event['external_id'];
        $job = $this->module->orderJob($id);
        $key = 'webhook:' . $event['id'];
        $db = Db::getInstance(); $shop = $this->module->shopId();
        $seen = $db->getValue('SELECT id FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE shop=' . $shop . ' AND event_key="' . pSQL($key) . '" AND status="done"');
        if (!$seen) {
            if ($event['type'] === 'order.refused') {
                // A refused attempt never cancels an accepted order or blocks a corrected retry.
                if ($job && empty($job['remote_id'])) {
                    $reason = json_encode(['reason' => 'fiscalm a refuzat o încercare de preluare. Verificați comanda în fiscalm.'], JSON_UNESCAPED_UNICODE);
                    $db->execute('UPDATE `' . _DB_PREFIX_ . 'fiscalm_job` SET result="' . pSQL($reason) . '" WHERE id=' . (int) $job['id'] . ' AND remote_id="" AND result IS NULL');
                }
            } else {
                if (!$job || empty($job['remote_id'])) { http_response_code(409); exit; }
                if (($event['order_id'] ?? '') !== $job['remote_id']) { http_response_code(400); exit; }
                if (!$this->module->refreshOrder($id)) { http_response_code(503); exit; }
            }
            $db->execute('INSERT IGNORE INTO `' . _DB_PREFIX_ . 'fiscalm_job` (shop,order_id,event_key,operation,payload,status,due) VALUES (' . $shop . ',' . $id . ',"' . pSQL($key) . '","webhook","{}","done",NOW())');
        }
        header('Content-Type: application/json'); echo '{"ok":true}'; exit;
    }
}
