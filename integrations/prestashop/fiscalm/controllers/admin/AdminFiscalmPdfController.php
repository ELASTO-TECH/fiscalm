<?php
/** SPDX-License-Identifier: AFL-3.0
 * Copyright 2026 fiscalm contributors. Licensed under the Academic Free License version 3.0. */
class AdminFiscalmPdfController extends ModuleAdminController
{
    public function postProcess()
    {
        if (!$this->context->employee->isLoggedBack() || !$this->access('view') || !$this->checkToken()) {
            http_response_code(403); exit;
        }
        $order = new Order((int) Tools::getValue('id_order'));
        if (!Validate::isLoadedObject($order) || (int) $order->id_shop !== (int) $this->context->shop->id) { http_response_code(404); exit; }
        $job = $this->module->orderJob((int) $order->id);
        $result = json_decode($job['result'] ?? '{}', true);
        if (empty($result['invoice_id'])) { http_response_code(404); exit; }
        $response = $this->module->client()->request('GET', '/invoicing/issued/' . rawurlencode($result['invoice_id']) . '/pdf', null, true);
        if ($response['status'] !== 200 || !str_starts_with($response['data'], '%PDF-')) { http_response_code(502); exit; }
        header('Content-Type: application/pdf'); header('Content-Disposition: inline; filename="factura.pdf"');
        header('Cache-Control: no-store'); header('X-Content-Type-Options: nosniff'); echo $response['data']; exit;
    }
}
