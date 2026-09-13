# kol.dwpc.com.cn access design

> **Superseded in part (2026-09-13):** the Basic Auth layer described below has been
> replaced by an in-application login page and signed session cookie. See
> `docs/plans/2026-09-13-web-login-design.md` for the replacement and
> `docs/web-login-status.md` for the cutover evidence. Everything else in this
> document (DNS, the vhost, TLS, the loopback-bound proxy target) still stands.

## Goal

Publish the existing Web role at `https://kol.dwpc.com.cn` without exposing its
loopback listener or changing application code.  The site must require an
independent HTTP Basic Auth credential before any application content is served.

## Chosen design

The DNS A record `kol.dwpc.com.cn -> 43.167.220.225` is already enabled.  Add a
single virtual host to the server's existing HTTPS reverse proxy:

1. Listen for `kol.dwpc.com.cn` on ports 80 and 443, using the proxy's existing
   ACME/Let's Encrypt mechanism for TLS.
2. Redirect HTTP to HTTPS.
3. Require Basic Auth, with a salted password hash stored in a root-readable
   proxy credential file; never put the cleartext password in the repository,
   service environment, or logs.
4. Proxy authenticated requests only to `http://127.0.0.1:8000` and preserve
   the forwarded scheme and host headers.
5. Leave the Web systemd unit loopback-bound; do not restart Web, ingest, or
   worker.  Validate the proxy configuration before a graceful proxy reload.

The pre-existing `codex-api.dwpc.com.cn` virtual host remains unchanged.  Both
hostnames may share the same public IP and TCP ports because the TLS Host/SNI
name selects the virtual host.

## Safety and rollback

First take a read-only snapshot of the active proxy configuration, listeners,
certificate tooling, and current `codex-api` response.  Install only the new
host-specific configuration and credential file.  If syntax validation,
certificate issuance, or end-to-end checks fail, remove the new host block and
gracefully reload the prior configuration.  No trading settings, database rows,
application sources, or Git deployment actions are in scope.

## Acceptance checks

- DNS resolves `kol.dwpc.com.cn` to `43.167.220.225`.
- `http://kol.dwpc.com.cn` redirects to HTTPS.
- An unauthenticated HTTPS request returns `401` without application content.
- An authenticated HTTPS request returns the Web page successfully.
- `codex-api.dwpc.com.cn` keeps its pre-change authenticated behavior and
  `codex-proxy` remains active on `127.0.0.1:3466`.
- `telegram-kol-web`, `telegram-kol-ingest`, and `telegram-kol-worker` retain
  their process identities throughout the change.
