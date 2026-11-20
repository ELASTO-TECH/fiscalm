<?php
/** SPDX-License-Identifier: AFL-3.0
 * Copyright 2026 fiscalm contributors. Licensed under the Academic Free License version 3.0. */
if (PHP_SAPI !== 'cli' || $argc !== 3) { exit(1); }
require rtrim($argv[1], '/') . '/config/config.inc.php';
global $kernel;
$kernel = class_exists('AdminKernel') ? new AdminKernel('prod', false) : new AppKernel('prod', false);
$kernel->boot();
Context::getContext()->currency = new Currency((int) Configuration::get('PS_CURRENCY_DEFAULT'));
Context::getContext()->language = new Language((int) Configuration::get('PS_LANG_DEFAULT'));
$shop = new Shop((int) $argv[2]);
if (!Validate::isLoadedObject($shop)) { exit(1); }
Shop::setContext(Shop::CONTEXT_SHOP, (int) $shop->id);
Context::getContext()->shop = $shop;
$module = Module::getInstanceByName('fiscalm');
if (!$module || !$module->active) { exit(1); }
$module->sweep();
$module->work();
$jobs = Db::getInstance()->executeS('SELECT order_id FROM `' . _DB_PREFIX_ . 'fiscalm_job` WHERE shop=' . (int) $shop->id . ' AND operation="order" AND status="done" AND due<=NOW() ORDER BY due,id LIMIT 10');
foreach ($jobs ?: [] as $job) { $module->refreshOrder((int) $job['order_id']); }
