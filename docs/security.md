# Ephemres Security Model, Threat Architecture & Defense Specification

**Document Version:** 3.3.2.v  
**Last Updated:** October 2026  
**Classification:** Public Security Specification & Threat Model  

---

## 1. Executive Summary & Security Philosophy

The **Ephemres Network Monitoring Platform** is engineered for mission-critical enterprise environments. Because network monitoring systems frequently reside inside privileged network segments (Management VLANs, NOC subnets, or Cloud VPCs with access to core infrastructure), Ephemres is designed under a **Zero-Trust, Defense-in-Depth** security philosophy:

1. **No Outbound Trust:** Outbound telemetry and alerting calls to the public internet are intercepted at the socket level to eliminate Server-Side Request Forgery (SSRF) and DNS rebinding risks.
2. **Zero Plaintext Secrets at Rest:** All third-party integration credentials (webhook URLs, SMTP passwords, custom authorization headers) are encrypted at rest using AES-256-GCM.
3. **Strict Ingress Isolation:** Internal database and cache services bind strictly to loopback interfaces; API gateways enforce reverse-proxy trust validation.
4. **Least-Privilege Execution:** Daemons execute under an unprivileged system service user with fine-grained Linux capabilities (`CAP_NET_RAW`), completely avoiding `root` execution.
5. **Bounded Cardinality Defense:** Ingested flow metrics undergo ephemeral port normalization and memory circuit-breaking to prevent resource exhaustion attacks.

---

## 2. Version Support Matrix

| Version | Release Type | Security Support Status | Recommended Action |
| :--- | :--- | :--- | :--- |
| **v3.3.2.v** | Vertical Integration Release | **Active / Current Standard** | Production standard with Vertical Integration Architecture & 12 zero-dependency engines. |
| **v3.3.0** | Feature & Ingestion Release | **Supported (Frozen Snapshot)** | Supported frozen production snapshot with Flow Exporter & Interface Telemetry. |
| **v3.1.35s** | Enterprise LTS Release | **Supported (Frozen Snapshot)** | Supported frozen long-term maintenance track. |
| **v3.3.1n** | Transitional Rebranding Release | **Unsupported / Superseded** | Transitional rebranding branch; upgrade directly to `v3.3.2.v`. |
| **<= v3.2.0** | Legacy Release | **Unsupported / Superseded** | Upgrade to `v3.3.2.v` recommended. |
| **< v3.1** | Legacy Alpha / Beta | **End of Life (EOL)** | Unmaintained; migrate to `v3.3.2.v` immediately. |

---

## 3. Network & Firewall Perimeter Architecture

```
                                      PUBLIC INTERNET / LAN
                                                │
                                                ▼
                                   ┌─────────────────────────┐
                                   │   Nginx Reverse Proxy   │ (Port 80 / 443)
                                   │ TLS 1.2/1.3 Termination │
                                   └────────────┬────────────┘
                                                │
                       ┌────────────────────────┼────────────────────────┐
                       │ (Port 8000)            │ (UDP 2055 / 4739)      │
                       ▼                        ▼                        ▼
              ┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
              │ Native ASGI Core │    │ Flow Collector   │    │  Traceroute/ICMP │
              │  (ephemres-api)  │    │ (ephemres-flowd) │    │(ephemres-engine) │
              └────────┬─────────┘    └────────┬─────────┘    └────────┬─────────┘
                       │                       │                       │
                       └───────────────────────┼───────────────────────┘
                                               │
                                               ▼
                              ┌──────────────────────────────────┐
                              │  Internal Loopback & FastStore   │
                              │  PostgreSQL (5432) / FastStore   │
                              └──────────────────────────────────┘
```

| Service Component | Port(s) | Binding Scope | Security Hardening Controls |
| :--- | :--- | :--- | :--- |
| **Nginx Web Server** | `80` / `443` | `0.0.0.0` (Public / LAN) | Public ingress point. Terminates TLS 1.2/1.3, sets `X-Frame-Options`, `X-Content-Type-Options` and `Referrer-Policy`, and proxies internal API traffic. HSTS is added by `ephemres-api` (see §9D); no Content-Security-Policy is set. |
| **Native ASGI Backend (`ephemres-api`)** | `8000` | `127.0.0.1` (Loopback Only) | Internal application server. Blocked from direct external access by firewall. |
| **Flow Collector (`ephemres-flowd`)** | `2055` / `4739` | UDP Management Ingress | Autonomous flow collector. 4MB kernel receive buffer, zero-allocation binary parsing, memory circuit breaker. |
| **PostgreSQL / TimescaleDB** | `5432` | `127.0.0.1` (Loopback Only) | Internal database. Protected by strong randomized password; strictly loopback bound. |
| **FastStore IPC** | `/dev/shm/ephemres_faststore_*.sqlite3` | UNIX Filesystem | Shared-memory WAL state store & NetFlow stream buffer (`0600 ephemres:ephemres`, no TCP port exposed). |
| **ICMP Ping Probes** | ICMP | Outbound | Low-frequency synthetic health probes (default 32s budget). |
| **Outbound Webhooks / Email** | `443` / `587` | Outbound Egress | Outbound notifications to Teams, Discord, Slack, and SMTP gateways. Protected by `AsyncHttpClient` SSRF guard. |

---

## 4. Outbound Egress & Zero-Trust SSRF Protection (CWE-918)

Ephemres incorporates **Zero-Trust Socket-Level SSRF Protection** (`SSRFSafeBackend`) and **Pre-Resolved Socket DNS Pinning** (`synthetic.py`), replacing traditional pre-flight hostname checks with kernel-boundary connection verification.

### A. The Threat: Time-of-Check to Time-of-Use (TOCTOU) DNS Rebinding
In standard application architectures, validating a URL before making an HTTP request leaves an exploitable window: an attacker configures a domain with a 0-second TTL that returns a public IP during pre-flight validation, but rebinds to `169.254.169.254` (cloud metadata) or `127.0.0.1` when the HTTP client establishes its TCP connection.

### B. The Defense: Millisecond-Level Socket Interception & Socket DNS Pinning
Ephemres implements a native zero-dependency HTTP/1.1 client (`app.core.http_client.AsyncHttpClient`) paired with pre-connect DNS resolution and IP validation (`validate_outbound_url` / `is_ip_safe`) that intercepts all outbound webhook and OIDC requests at the socket layer **before** `asyncio.open_connection()` is invoked:

1. **Connection-Time Resolution & Pinning:** Performs an asynchronous DNS resolution immediately prior to opening sockets, and pins physical connections directly to the resolved IP.
2. **Strict IP Enforcement:** Evaluates all returned addresses against the global routable IP specification:
   - **RFC 1918 Private Subnets:** `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` are rejected for generic webhooks. *(Note: Permitted specifically for `EMAIL_SMTP` channels to support internal corporate relays, and synthetic probes targeting internal corporate appliances).*
   - **RFC 6598 Carrier-Grade NAT (CGNAT):** `100.64.0.0/10` is explicitly blocked to protect internal Kubernetes pod networks, AWS VPC CNI overlays, and Tailscale/WireGuard meshes.
   - **Loopback & Localhost:** `127.0.0.0/8` and `::1/128` are rejected for outbound webhooks.
   - **Cloud Metadata Services:** `169.254.169.254` (AWS/GCP/Azure link-local metadata) and `fd00:ec2::254` (AWS IPv6 metadata) are **strictly and unconditionally rejected across all channels, including SMTP**.
   - **IPv4-Mapped IPv6:** Encodings such as `::ffff:127.0.0.1` or `::ffff:100.64.0.1` are unmapped and strictly evaluated.
   - **Unix Sockets:** Direct Unix domain socket connections (`connect_unix_socket`) are completely disabled.
3. **Immediate Kernel Abort:** If an address resolves to a forbidden or private range, the TCP connection is aborted before sending a single HTTP byte, rendering DNS rebinding attacks physically impossible.
4. **Redirect Lockdown:** All outbound HTTP clients enforce `follow_redirects=False`. Third-party endpoints cannot bounce requests into internal networks using HTTP 301/302 redirects.

---

## 5. Cryptographic Architecture & Secrets Governance

### A. AES-256-GCM Encryption at Rest
All sensitive configuration attributes stored in PostgreSQL (webhook URLs, SMTP passwords, custom authorization headers) are encrypted at rest using **AES-256-GCM** (Galois/Counter Mode) authenticated encryption:
* **Key Derivation:** Master keys are derived using **HKDF-SHA256** (RFC 5869) with a domain-separated salt (`ephemres-alert-crypto-salt-v1`, with transparent fallback decryption for legacy `lnmp-alert-crypto-salt-v1` payloads).
* **Random Initialization Vectors:** Every encrypted value receives a cryptographically secure, unique 12-byte random IV (`os.urandom(12)`).
* **Cryptographic Tamper Resistance:** Decryption strictly validates the 16-byte GCM authentication tag. Any tampering or bit-flipping in the database raises an immediate authentication failure.
* **Storage Format:** Ciphertexts are stored with an explicit version envelope: `ENC:v1:<base64-iv>:<base64-ciphertext-and-tag>`.

### B. Host-Level Environment File Protection
The master encryption key (`EPHEMRES_SECRET_KEY`) resides in `/etc/ephemres/ephemres.env`. 
* The production installer (`deploy/install.sh`) and upgrade utility (`deploy/upgrade.sh`) automatically enforce `chmod 0600` and `chown ephemres:ephemres` on this file.
* Secrets are never stored in git or checked into repository assets.

### C. Secret Masking at the API Boundary
When an authenticated administrator views alert channels (`GET /api/v1/alerts/channels`), Ephemres executes context-aware secret masking:
* **Microsoft Teams Webhooks:** Incoming webhook path tokens (`/IncomingWebhook/<token1>/<token2>`) are masked to `••••••••••••`.
* **Discord & Slack Webhooks:** Webhook path tokens (`/api/webhooks/...` and `/services/T...`) are masked.
* **Basic Auth Credentials:** URLs containing embedded credentials (`https://user:password@host/path`) are sanitized to `https://user:••••••••@host/path`.
* **Sensitive Query Parameters:** Query parameters matching `sig`, `token`, `key`, `secret`, `webhook`, `auth`, `api_key`, `password`, `access_token`, `apikey`, `bearer`, or `code` are masked.
* **Header Preservation:** Custom headers (e.g. `Authorization: Bearer <token>`) are masked in the UI. When an administrator saves channel modifications, existing secrets are preserved without overwriting them with bullet masks.

---

## 6. Authentication, Session Governance & Multi-Worker State

### A. JWT in Secure HttpOnly Cookies
* **XSS Defense:** Authentication tokens are stored inside browser `HttpOnly`, `SameSite=Lax`, and `Secure` (in HTTPS production) cookies. Malicious client-side JavaScript cannot read session tokens.
* **Cryptographic JTI Enforcement:** Every issued token carries a cryptographically unique UUID `jti` (JWT ID). Tokens lacking a valid `jti` claim are rejected immediately.
* **Session Lifetime:** Access tokens expire `session_timeout` minutes after login (default 120). The value comes from **Admin Settings → Security & Discovery**, falling back to `[security] session_timeout_minutes` in `config.toml`.

### B. Distributed Multi-Worker Session Governance
In production, `ephemres-api` runs with multiple Uvicorn workers (`--workers 2`). Ephemres prevents single-worker auth bypasses via distributed session drivers:
* **Driver Architecture:** Session validity is verified asynchronously against `PostgresSessionStore` or `FastStoreSessionStore` (`backend/app/core/faststore.py`) rather than volatile single-worker process memory.
* **Concurrent Device Quota:** Accounts are limited to `[security] max_active_sessions_per_user` concurrent sessions (default 2, range 1–10). A further login evicts the oldest session (FIFO).
* **Instantaneous Session Revocation:** Changing an account password or clicking "Sign Out Everywhere" immediately deletes active session records from the datastore, revoking access across all workers in real time.
* **Forced Initial Password Reset:** Newly provisioned user accounts (`ADMIN` or `VIEWER`) must change their temporary password upon first login before accessing system telemetry.

### C. Password Hashing
* **Password Hashing**: Passwords are hashed using Python's native OpenSSL-backed **`hashlib.scrypt`** (`$scrypt$n=131072,r=8,p=1$`, 16-byte cryptographic salt, 64-byte derived key) verified in constant time via `hmac.compare_digest`. Hashes created with the earlier `n=16384` cost still verify and are transparently re-hashed at the stronger cost on the user's next successful login. Legacy `$argon2id$` hashes from pre-`v3.3.2v` backups are automatically invalidated and flagged for password reset during cross-version restore.
* Plaintext passwords are never logged or stored.

---

## 7. Network Trust & Attribution Defense

### A. Strict Reverse-Proxy Trust
When determining the client's real IP address for audit logs and security policies, `DEFAULT_TRUSTED_PROXIES` is strictly limited to loopback networks:
```
127.0.0.0/8 (IPv4 Loopback)
::1/128     (IPv6 Loopback)
```
* Ephemres will **only** parse `X-Forwarded-For` or `X-Real-IP` headers if the direct TCP peer connection originates from a trusted local proxy (Nginx).
* Direct external requests attempting to spoof `X-Forwarded-For: 127.0.0.1` are rejected, preventing bypass of brute-force shields and IP audit spoofing.

### B. IP-Scoped Brute-Force Lockouts
* **Threshold:** After `lockout_threshold` failed attempts (default 5; **Admin Settings → Security & Discovery**) the `IP:username` pair is locked for 15 minutes. The failure counter resets after 30 minutes without further failures.
* **NAT / VPN Isolation:** Lockout keys are scoped by `f"{client_ip}:{username}"`. If an external attacker attempts to brute force an administrator account, only the attacker's IP is locked out; legitimate administrators on corporate networks remain unaffected.

---

## 8. Input Sanitization & Attack Mitigations

### A. SQL Injection Neutralization
* 100% of database interactions execute through **app.core.orm parameterized queries ($1, $2 positional asyncpg bindings)** or explicit type-casted bindings (`CAST($1 AS uuid)`).
* Raw string concatenation into SQL statements is strictly prohibited.

### B. CSV Formula Injection Defense (Spreadsheet Execution)
* Telemetry exports stream large datasets that operators open in Microsoft Excel or LibreOffice.
* **Formula Defense:** In `backend/app/routers/reports.py`, any cell value starting with `=`, `+`, `-`, `@`, `\t`, or `\r` is automatically prefixed with a single quote (`'`), neutralizing spreadsheet formula execution.

### C. Notification Payload Sanitization
* **HTML Injection in Email:** In `backend/app/services/alert_formatters.py`, all dynamic endpoint parameters (`endpoint_name`, `ip_address`, `event_type`) are sanitized with `html.escape()`.
* **CRLF Email Header Injection:** All email `Subject`, `To`, and `From` headers are sanitized to strip carriage returns (`\r`) and line feeds (`\n`), preventing Bcc injection or mail gateway manipulation.
* **Discord Mention Storms:** Discord webhooks enforce `"allowed_mentions": {"parse": []}`, preventing malicious hostnames (e.g. `@everyone` or `@here`) from triggering server-wide notification storms.
* **Slack MRKDWN Escaping:** Dynamic strings in Slack Block Kit payloads escape special formatting characters (`<`, `>`, `&`).

### D. Zero-Dependency Binary PDF Emitter (`app.core.pdf`)
Ephemres eliminates external HTML-to-PDF rendering engines (`WeasyPrint`, `wkhtmltopdf`, headless browsers) and their associated Local File Inclusion (LFI) / SSRF attack surface.
* Executive SLA reports are compiled directly into raw **PDF 1.4 binary streams** in memory using `app.core.pdf`.
* Organization logos are accepted only as PNG or JPEG data URIs (the declared type and the file's magic bytes must match) and embedded directly as native PDF `/Subtype /Image` XObjects.
* Resource loading during report generation (`app.core.pdf`) allows `data:` URIs, plus local files only inside `backend/app/templates/reports/` and `frontend/src/assets/`; `http`, `https`, `ftp`, and any other scheme are refused, as is any path outside those two directories.

### E. Cryptographic Integrity Verification (`X-Report-SHA256`)
Every generated PDF byte stream is hashed prior to transmission:
* **SHA-256 Digest Header**: The HTTP response attaches an `X-Report-SHA256` header containing the hex-encoded SHA-256 digest of the exact PDF payload so auditors can verify document authenticity and detect post-export tampering.

---

## 9. Operating System & Privilege Hardening

### A. Non-Root Daemon Execution
* `ephemres-api`, `ephemres-engine`, and `ephemres-flowd` run under the dedicated unprivileged system user `ephemres` (UID/GID isolated).
* The only elevated action available to the service user is a sudoers rule (`/etc/sudoers.d/ephemres`, mode `0440`) allowing `systemctl start|stop|restart ephemres-flowd`, used when an administrator toggles flow ingestion in the Settings UI.

### B. Linux Capability Grants (`CAP_NET_RAW`)
* Raw socket creation for ICMP echo requests (ping) and traceroute normally requires root privileges.
* The systemd units grant `CAP_NET_RAW` to each daemon process through `AmbientCapabilities`, so no file capability is placed on the Python interpreter:

  | Unit | `AmbientCapabilities` | `CapabilityBoundingSet` |
  | :--- | :--- | :--- |
  | `ephemres-engine` | `CAP_NET_RAW` | `CAP_NET_RAW` |
  | `ephemres-flowd` | `CAP_NET_RAW`, `CAP_NET_BIND_SERVICE` | `CAP_NET_RAW`, `CAP_NET_BIND_SERVICE` |
  | `ephemres-api` | `CAP_NET_RAW` | not restricted: the flowd `sudo` rule in §9A needs `CAP_SETUID`/`CAP_SETGID` |

* `deploy/install.sh` and `deploy/upgrade.sh` set a file capability only on the dedicated `traceroute` binary (`setcap cap_net_raw+ep "$(command -v traceroute)"`), used as a fallback when in-process raw-socket tracing is unavailable.
* Releases before this change also ran `setcap cap_net_raw+ep` on the resolved virtualenv interpreter. That interpreter is the system-wide Python (for example `/usr/bin/python3.12`), so the capability applied to every Python process on the host. `deploy/upgrade.sh` now removes it when it is exactly `cap_net_raw` (the value the installer set) and leaves any other file capabilities untouched.

### C. Systemd Service Isolation
The production unit files (`deploy/ephemres-api.service`, `deploy/ephemres-engine.service`, and `deploy/ephemres-flowd.service`) currently apply:
* `User=ephemres` / `Group=ephemres`: daemons never run as root.
* `RuntimeDirectory=ephemres` with `RuntimeDirectoryMode=0700`: a private runtime directory under `/run/ephemres`.
* `Type=notify` with `WatchdogSec=45`: systemd restarts a daemon that stops sending watchdog heartbeats.

Filesystem sandboxing directives (`ProtectSystem=`, `PrivateTmp=`, `NoNewPrivileges=`) are **not** enabled in the shipped units. Note that `NoNewPrivileges=true` would also stop the file capabilities in §9B from taking effect, so it can only be added together with relying solely on the units' ambient capabilities.

### D. HTTP Strict Transport Security (HSTS)
* `ephemres-api` adds `Strict-Transport-Security: max-age=31536000; includeSubDomains` to its responses when `[security] hsts_enabled = true` in `/etc/ephemres/config.toml`. The shipped config template enables it; the built-in default when the key is absent is `false`.
* The same flag marks the session cookie `Secure`.
* Static frontend assets are served directly by Nginx, which does not add HSTS in the shipped `deploy/nginx.conf.template`; add `add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;` to the TLS server block to cover them.

---

## 10. Role-Based Access Control (RBAC) Matrix

Ephemres enforces a strict, two-tier role architecture (`ADMIN` and `VIEWER`), seeded directly in the database:

| Resource / Action | Unauthenticated | Viewer (`VIEWER`) | Administrator (`ADMIN`) |
| :--- | :--- | :--- | :--- |
| **View Dashboard & Telemetry** | ❌ (Redirect Login) | ✅ | ✅ |
| **Inspect Topology Map** | ❌ | ✅ | ✅ |
| **Export Telemetry CSV** | ❌ | ✅ | ✅ |
| **Acknowledge Incidents** | ❌ | ❌ | ✅ |
| **Create / Modify Endpoints** | ❌ | ❌ | ✅ |
| **Configure Alert Channels** | ❌ | ❌ | ✅ |
| **Send Diagnostic Test Alerts** | ❌ | ❌ | ✅ |
| **View Alert Delivery Audit Logs** | ❌ | ❌ | ✅ |
| **User & Account Management** | ❌ | ❌ | ✅ |
| **System Settings Modification** | ❌ | ❌ | ✅ |

---

## 11. Security Audit & Compliance Checklist for Administrators

When deploying Ephemres in production, ensure the following hardening steps are verified:

- [ ] **HTTPS Enforced:** Nginx configured with valid SSL/TLS certificates, `[security] hsts_enabled = true` in `config.toml`, and (optionally) an HSTS `add_header` in the Nginx TLS server block for static assets.
- [ ] **File Permissions:** Verify `/etc/ephemres/ephemres.env` has permissions `0600` owned by `ephemres:ephemres`.
- [ ] **Database & IPC Isolation:** Ensure PostgreSQL (`5432`) listens strictly on `127.0.0.1` and FastStore shared-memory files (`/dev/shm/ephemres_faststore*`) are owned by `ephemres:ephemres` (`0600`).
- [ ] **Admin Password Changed:** Verify the default administrator password was changed upon initial login.
- [ ] **Firewall Egress Rules:** Restrict outbound egress from the Ephemres server strictly to necessary destinations (Port 443 for webhooks, Port 587 for SMTP).

---

## 12. Vulnerability Reporting & Coordinated Disclosure

If you discover a security vulnerability or potential threat in Ephemres, please disclose it responsibly:

* **Security Contact:** `dagera07@gmail.com` or via **[GitHub Private Vulnerability Reporting](https://github.com/dc8official/ephemres/security/advisories/new)** on the repository (`https://github.com/dc8official/ephemres`).
* **Policy:** Please do not file public GitHub issues for security vulnerabilities.
* **Response SLA:** The Ephemres security team acknowledges vulnerability reports within **24 hours** and aims to deliver patches within **72 hours** for high or critical severity findings.
