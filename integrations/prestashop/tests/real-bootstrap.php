<?php
require '/var/www/html/config/config.inc.php';
global $kernel;
$kernel = class_exists('AdminKernel') ? new AdminKernel('prod', false) : new AppKernel('prod', false);
$kernel->boot();
$context = Context::getContext(); $context->shop = new Shop(1); Shop::setContext(Shop::CONTEXT_SHOP, 1);
$context->language = new Language((int) Configuration::get('PS_LANG_DEFAULT'));
$context->currency = new Currency((int) Configuration::get('PS_CURRENCY_DEFAULT'));
$module = Module::getInstanceByName('fiscalm'); $db = Db::getInstance();
$id = (int) $db->getValue('SELECT id_order FROM `' . _DB_PREFIX_ . 'orders` ORDER BY id_order');
