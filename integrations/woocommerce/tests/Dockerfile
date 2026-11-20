ARG PHP_VERSION=8.3
FROM composer:2 AS composer
FROM wordpress:php${PHP_VERSION}-apache
COPY --from=composer /usr/bin/composer /usr/local/bin/composer
RUN apt-get update && apt-get install -y --no-install-recommends unzip git libonig-dev && docker-php-ext-install mbstring && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://github.com/wp-cli/wp-cli/releases/download/v2.12.0/wp-cli-2.12.0.phar -o /usr/local/bin/wp && echo 'ce34ddd838f7351d6759068d09793f26755463b4a4610a5a5c0a97b68220d85c  /usr/local/bin/wp' | sha256sum -c - && chmod +x /usr/local/bin/wp
