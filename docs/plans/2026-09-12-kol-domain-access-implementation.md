# kol.dwpc.com.cn access Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Publish the existing loopback-only Web role at `https://kol.dwpc.com.cn` behind TLS and an independent Basic Auth boundary.

**Architecture:** The current reverse proxy remains the only public listener on ports 80/443. A host-specific virtual host authenticates requests before proxying to `127.0.0.1:8000`; the Telegram application units and `codex-api` configuration remain untouched. The proxy's existing ACME mechanism obtains the certificate.

**Tech Stack:** Existing server reverse proxy and ACME client, systemd, `curl`, `openssl`, DNS A record, Telegram KOL Web on `127.0.0.1:8000`.

---

### Task 1: Capture the live proxy baseline

**Files:**
- Read: active server proxy configuration and systemd units
- Create: root-owned server evidence file (outside the repository)

**Step 1: Confirm DNS and external ownership boundary**

Run from the server and a public resolver:

```bash
dig +short A kol.dwpc.com.cn
ss -ltnp '( sport = :80 or sport = :443 or sport = :8000 or sport = :3466 )'
systemctl is-active nginx caddy apache2 codex-proxy telegram-kol-web telegram-kol-ingest telegram-kol-worker
```

Expected: `kol.dwpc.com.cn` resolves to `43.167.220.225`; Web and proxy backends are loopback-bound; identify exactly one active front proxy.

**Step 2: Record a before-state response for the existing proxy API**

```bash
curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' https://codex-api.dwpc.com.cn/v1/models
```

Expected: record the code only; do not print credentials or response bodies.

**Step 3: Stop on an unsupported topology**

If neither Nginx, Caddy, nor Apache is the active public proxy, or another site already owns `kol.dwpc.com.cn`, stop and report the exact topology. Do not install a second proxy.

### Task 2: Add the isolated virtual host

**Files:**
- Create: the active proxy's host-specific `kol.dwpc.com.cn` configuration in its standard include directory
- Create: root-readable Basic Auth hash file outside the repository
- Do not modify: application source, project systemd units, `codex-api` virtual host

**Step 1: Generate a secret without persisting it in shell history or the repository**

Interactively generate a random password and store only its salted hash in the proxy's credential file with mode `0600`. Deliver the cleartext password only to the account owner through the local session, never logs or Telegram.

**Step 2: Add the minimal proxy configuration**

The configuration must contain this behavior, expressed in the detected proxy's native syntax:

```text
HTTP kol.dwpc.com.cn -> HTTPS kol.dwpc.com.cn
HTTPS kol.dwpc.com.cn -> Basic Auth -> http://127.0.0.1:8000
```

Forward `Host`, request scheme, and client address headers; preserve WebSocket upgrade headers if the existing proxy convention uses them. Never proxy to a public `:8000` address.

**Step 3: Run the configuration test before activation**

Run the detected proxy's non-mutating syntax check (`nginx -t`, `caddy validate`, or Apache equivalent).

Expected: success. On any failure, remove only the new host configuration and leave the current proxy running.

### Task 3: Activate TLS and verify the security boundary

**Files:**
- Modify: only the new proxy configuration, if ACME reports a required host-specific adjustment

**Step 1: Gracefully reload only the active reverse proxy**

Use the proxy's reload action, not a restart. Do not restart `codex-proxy`, Web, ingest, or worker.

**Step 2: Validate redirect and unauthenticated denial**

```bash
curl --silent --show-error --output /dev/null --write-out '%{http_code} %{redirect_url}\n' http://kol.dwpc.com.cn/
curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' https://kol.dwpc.com.cn/
```

Expected: HTTP redirects to HTTPS; HTTPS returns `401`.

**Step 3: Validate authenticated upstream delivery without logging the credential**

Use an interactive credential input and request `https://kol.dwpc.com.cn/`.

Expected: TLS verification succeeds and response is `200` with the Web HTML.

**Step 4: Regression checks**

```bash
systemctl is-active codex-proxy telegram-kol-web telegram-kol-ingest telegram-kol-worker
curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' https://codex-api.dwpc.com.cn/v1/models
```

Expected: all services retain `active`; the existing API's code matches Task 1.

### Task 4: Rollback on any failed acceptance check

**Files:**
- Remove: only the new `kol.dwpc.com.cn` proxy configuration and Basic Auth file

**Step 1: Disable only the new host configuration**

Remove or unlink only the `kol.dwpc.com.cn` virtual-host config; leave DNS in place for diagnosis.

**Step 2: Validate then gracefully reload the proxy**

Run its configuration test followed by a graceful reload.

**Step 3: Confirm unaffected services**

Repeat Task 3's regression checks and record the failure reason in the root-owned evidence file.
