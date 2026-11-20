<?php
/** SPDX-License-Identifier: AFL-3.0
 * Copyright 2026 fiscalm contributors. Licensed under the Academic Free License version 3.0. */
declare(strict_types=1);

final class FiscalmMapper
{
    public static function decimal($value): string
    {
        $value = (string) $value;
        if (!preg_match('/^-?\d+(?:\.\d{1,6})?$/D', $value)) {
            throw new DomainException('Valoare zecimală invalidă.');
        }
        return $value;
    }

    public static function round(string $value, int $scale = 2): string
    {
        return bcadd($value, bccomp($value, '0', 8) < 0 ? '-0.005' : '0.005', $scale);
    }

    /** Preserve line tax bases; refuse discounts without an unambiguous VAT allocation. */
    public static function map(array $order, string $channel): array
    {
        $lines = [];
        $total = '0';
        foreach ($order['lines'] as $line) {
            foreach (['quantity', 'unit_price', 'vat_rate'] as $key) {
                $line[$key] = self::decimal($line[$key]);
            }
            if (bccomp($line['quantity'], '0', 6) <= 0 || bccomp($line['vat_rate'], '0', 6) < 0) {
                throw new DomainException('Cantitate sau cotă TVA invalidă.');
            }
            $amount = bcmul($line['quantity'], $line['unit_price'], 8);
            if (!$order['prices_include_vat']) {
                $amount = bcmul($amount, bcadd('1', bcdiv($line['vat_rate'], '100', 8), 8), 8);
            }
            $total = bcadd($total, self::round($amount), 2);
            $lines[] = $line;
        }
        if (!$lines || bccomp($total, self::round(self::decimal($order['total'])), 2) !== 0) {
            throw new DomainException('Totalul comenzii nu corespunde liniilor. Verificați reducerile și TVA.');
        }
        $buyer = $order['buyer'];
        if ($buyer['type'] === 'company' && empty($buyer['cui'])) {
            throw new DomainException('Completați CUI pentru facturarea firmei.');
        }
        return array_filter([
            'channel' => $channel, 'external_id' => (string) $order['id'],
            'placed_at' => substr($order['date'], 0, 10), 'currency' => $order['currency'],
            'prices_include_vat' => $order['prices_include_vat'], 'buyer' => $buyer,
            'lines' => $lines, 'payment' => $order['payment'], 'awb' => $order['awb'] ?? null,
        ], static fn($value) => $value !== null);
    }

    /** The API supports whole invoice positions, never a guessed quantity credit. */
    public static function refund(array $positions, array $quantities, array $refunded): array
    {
        $lines = [];
        foreach ($refunded as $detail => $quantity) {
            if (!isset($positions[$detail], $quantities[$detail]) || !ctype_digit((string) $quantity) || (int) $quantity <= 0 || (int) $quantity !== (int) $quantities[$detail]) {
                throw new DomainException('Returul parțial necesită verificare în fiscalm.');
            }
            $lines[] = (int) $positions[$detail];
        }
        if (!$lines) {
            throw new DomainException('Returul nu conține linii.');
        }
        return ['lines' => $lines];
    }

    public static function verifyWebhook(string $secret, string $timestamp, string $body, string $signature, int $now): bool
    {
        return preg_match('/^sha256=([a-f0-9]{64})$/D', $signature, $match) === 1
            && strlen($secret) >= 32 && ctype_digit($timestamp) && abs($now - (int) $timestamp) <= 300
            && hash_equals(hash_hmac('sha256', $timestamp . '.' . $body, $secret), $match[1]);
    }
}
