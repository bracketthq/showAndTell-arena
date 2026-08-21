<?php

// The pinned local Dovecot testing image generates an ephemeral self-signed
// certificate. Encryption is still required; peer verification is disabled
// only for the compose-internal `dovecot` endpoint.
$config['imap_conn_options'] = [
    'ssl' => [
        'verify_peer' => false,
        'verify_peer_name' => false,
        'allow_self_signed' => true,
    ],
];
$config['smtp_conn_options'] = $config['imap_conn_options'];

// Mailpit is an isolated compose-internal capture SMTP server. Roundcube's
// defaults forward the IMAP username/password to SMTP, but this local service
// intentionally has no authentication.
$config['smtp_user'] = '';
$config['smtp_pass'] = '';
