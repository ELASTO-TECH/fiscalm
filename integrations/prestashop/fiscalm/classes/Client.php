<?php
/** SPDX-License-Identifier: AFL-3.0
 * Copyright 2026 fiscalm contributors. Licensed under the Academic Free License version 3.0. */
declare(strict_types=1);

final class FiscalmClient
{
    public function __construct(private string $base, private string $token, private string $company)
    {
        $url = parse_url($base);
        if (!$url || !in_array($url['scheme'] ?? '', ['https'], true) || empty($url['host'])
            || (isset($url['user']) || isset($url['pass'])) || isset($url['query']) || isset($url['fragment'])) {
            // HTTP is restricted to the explicitly opted-in, isolated Docker test network.
            if (!(getenv('FISCALM_E2E') === '1' && in_array($base, ['http://mock:8080', 'http://fiscalm:8790'], true))) {
                throw new DomainException('Adresa fiscalm trebuie să folosească HTTPS.');
            }
        }
        if (preg_match('/[\r\n]/', $token) || $token === '') {
            throw new DomainException('Completați cheia API.');
        }
    }

    public function request(string $method, string $path, ?array $body = null, bool $pdf = false): array
    {
        $handle = curl_init(rtrim($this->base, '/') . '/api/v1/companies/' . rawurlencode($this->company) . $path);
        $bytes = '';
        curl_setopt_array($handle, [
            CURLOPT_CUSTOMREQUEST => $method, CURLOPT_HTTPHEADER => ['Authorization: Bearer ' . $this->token,
                'Content-Type: application/json', 'Accept: ' . ($pdf ? 'application/pdf' : 'application/json')],
            CURLOPT_FOLLOWLOCATION => false, CURLOPT_CONNECTTIMEOUT => 5, CURLOPT_TIMEOUT => 20,
            CURLOPT_PROTOCOLS => CURLPROTO_HTTP | CURLPROTO_HTTPS,
            CURLOPT_WRITEFUNCTION => static function ($handle, string $chunk) use (&$bytes): int {
                if (strlen($bytes) + strlen($chunk) > 10000000) { return 0; }
                $bytes .= $chunk;
                return strlen($chunk);
            },
        ]);
        if ($body !== null) {
            curl_setopt($handle, CURLOPT_POSTFIELDS, json_encode($body ?: (object) [], JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR));
        }
        $ok = curl_exec($handle);
        $status = (int) curl_getinfo($handle, CURLINFO_RESPONSE_CODE);
        curl_close($handle);
        if ($ok === false) { return ['status' => 0, 'data' => []]; }
        return ['status' => $status, 'data' => $pdf ? $bytes : (json_decode($bytes, true) ?: [])];
    }
}
