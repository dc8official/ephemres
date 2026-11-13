# Ephemres Security Model, Threat Architecture & Defense Specification

**Document Version:** 3.3.5  
**Last Updated:** October 2026  
**Classification:** Public Security Specification & Threat Model  

---

## 1. Executive Summary & Security Philosophy

The **Ephemres Network Monitoring Platform** is engineered for mission-critical enterprise environments. Because network monitoring systems frequently reside inside privileged network segments (Management VLANs, NOC subnets, or Cloud VPCs with access to core infrastructure), Ephemres is designed under a **Zero-Trust, Defense-in-Depth** security philosophy:

1. **No Outbound Trust:** Outbound telemetry and alerting calls to the public internet are intercepted at the socket level to eliminate Server-Side Request Forgery (SSRF) and DNS rebinding risks.
2. **Zero Plaintext Secrets at Rest:** All third-party integration credentials (webhook URLs, SMTP passwords, custom authorization headers) are encrypted at rest using AES-256-GCM.
3. **Strict Ingress Isolation:** Internal database and cache services bind strictly to loopback interfaces; API gateways enforce reverse-proxy trust validation.
4. **Least-Privilege Execution:** Daemons execute under an unprivileged system service user with fine-grained Linux capabilities (`CAP_NET_RAW`), completely avoiding `root` execution.
5. **Bounded Cardinality Defense:** Ingested flow metrics undergo ephemeral port normalization, packets from unenrolled exporters are dropped before parsing, and flows held in memory are capped, to prevent resource exhaustion attacks.

---

## 2. Version Support Matrix

| Version | Release Type | Security Support Status | Recommended Action |
| :--- | :--- | :--- | :--- |
| **v3.3.5** | Feature, Telemetry & Route Augmentation Release | **Active / Current Standard** | Current production standard with Traffic Analytics, Per-Cycle Probe History, WAN Edge Totals, and NetFlow Route Augmentation. |
| **v3.3.2.v** | Vertical Integration Release | **Supported (Production Ready)** | Supported zero-dependency native core release; upgrading to `v3.3.5` is recommended, though `v3.3.2.v` remains safe and supported in production. |
| **v3.3.0** | Enterprise LTS Release | **Supported (Long-Term Support / LTS)** | Supported long-term maintenance track with Flow Exporter & Interface Telemetry. |
| **v3.1.35s** | Legacy Release | **Unsupported / End of Support** | No longer supported; upgrade to `v3.3.0` (LTS) or the newest release (`v3.3.5`). |
| **v3.3.1n** | Transitional Rebranding Release | **Unsupported / Superseded** | Transitional rebranding branch; upgrade directly to `v3.3.5`. |
| **<= v3.2.0** | Legacy Release | **Unsupported / Superseded** | Upgrade to `v3.3.0` (LTS) or `v3.3.5` recommended. |
| **< v3.1** | Legacy Alpha / Beta | **End of Life (EOL)** | Unmaintained; migrate to `v3.3.5` immediately. |

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
| **Flow Collector (`ephemres-flowd`)** | `2055` / `4739` | UDP Management Ingress | Autonomous flow collector. 4MB kernel receive buffer (granted because the installer raises `net.core.rmem_max` with `/etc/sysctl.d/90-ephemres.conf`), zero-allocation binary parsing; packets from unenrolled exporters are dropped before parsing; flows held for the database are capped by `flow.buffer_memory_limit_mb`. |
| **PostgreSQL / TimescaleDB** | `5432` | `127.0.0.1` (Loopback Only) | Internal database. Protected by strong randomized password; strictly loopback bound. |
| **FastStore IPC** | `/run/ephemres/ephemres_faststore_*.sqlite3` (`/dev/shm` fallback) | UNIX Filesystem | In-memory SQLite store (`0600 ephemres:ephemres`, no TCP port exposed) for live events between processes (when Performance Mode is on) and flowd's exporter discovery and interface lists. Sessions and sign-in lockouts are kept in PostgreSQL (`user_sessions`, `auth_login_failures`). Flows never pass through it. |
| **ICMP Ping Probes** | ICMP | Outbound | Low-frequency health probes (default 60s cycle budget). |
| **Outbound Webhooks / Email** | `443` / `587` | Outbound Egress | Outbound notifications to Teams, Discord, Slack, and SMTP gateways. Protected by `AsyncHttpClient` SSRF guard. |

---

## 4. Outbound Egress & Zero-Trust SSRF Protection (CWE-918)

Ephemres incorporates **Zero-Trust Socket-Level SSRF Protection** (`SSRFSafeBackend`) and **Pre-Resolved Socket DNS Pinning** (`synthetic.py`), replacing traditional pre-flight hostname checks with kernel-boundary connection verification.

### A. The Threat: Time-of-Check to Time-of-Use (TOCTOU) DNS Rebinding
In standard application architectures, validating a URL before making an HTTP request leaves an exploitable window: an attacker configures a domain with a 0-second TTL that returns a public IP during pre-flight validation, but rebinds to `169.254.169.254` (cloud metadata) or `127.0.0.1` when the HTTP client establishes its TCP connection.

### B. The Defense: Millisecond-Level Socket Interception & Socket DNS Pinning
Ephemres implements a native zero-dependency HTTP/1.1 client (`app.core.http_client.AsyncHttpClient`) paired with pre-connect DNS resolution and IP validation (`validate_outbound_url` / `is_ip_safe`) that intercepts all outbound webhook and alert dispatch requests at the socket layer **before** `asyncio.open_connection()` is invoked:

1. **Connection-Time Resolution & Pinning:** Performs an asynchronous DNS resolution immediately prior to opening sockets, and pins physical connections directly to the resolved IP.
2. **Strict IP Enforcement:** Evaluates all returned addresses against the global routable IP specification:
   - **RFC 1918 Private Subnets:** `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` are rejected for generic webhooks. *(Note: Permitted specifically for `EMAIL_SMTP` channels to support internal corporate relays, and synthetic probes targeting internal corporate appliances).*
   - **RFC 6598 Carrier-Grade NAT (CGNAT):** `100.64.0.0/10` is explicitly blocked to protect internal Kubernetes pod networks, AWS VPC CNI overlays, and Tailscale/WireGuard meshes.
   - **Loopback & Localhost:** `127.0.0.0/8` and `::1/128` are rejected for outbound webhooks (permitted for `EMAIL_SMTP` when targeting a local MTA relay).
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

### D. SNMP Credentials (v3.3.6)
* **Storage:** SNMP communities and v3 passwords are stored as one AES-256-GCM value per credential set (`snmp_credentials.secrets`), like alert channel secrets, and restored with re-encryption.
* **Write-only:** the API never returns them, only whether each is set; leaving a field empty when editing keeps the stored value.
* **Audit:** changes are audited without the secrets.
* **Test button:** admin only, at most 6 runs a minute per API worker, and audited (`SNMP:TEST` with the address). It sends SNMP from the server to any address an admin types.
* **Read-only:** the poller and the survey only read (GET, GETNEXT, GETBULK); there is no SET and no traps.
* **Recommended:** SNMPv3 with SHA-2 and AES on devices, and an ACL that allows only the Ephemres server.

---

## 6. Authentication, Session Governance & Multi-Worker State

### A. JWT in Secure HttpOnly Cookies
* **XSS Defense:** Authentication tokens are stored inside browser `HttpOnly`, `SameSite=Lax`, and `Secure` (in HTTPS production) cookies. Malicious client-side JavaScript cannot read session tokens.
* **Cryptographic JTI Enforcement:** Every issued token carries a cryptographically unique 32-character hex `jti` (JWT ID, generated via `secrets.token_hex(16)`). Tokens lacking a valid `jti` claim are rejected immediately.
* **Session Lifetime (v3.3.6):** A session ends after the account's own idle timeout (`users.session_idle_minutes`: admin 15, 30 or 60 minutes; viewer 30 minutes to 4 hours), counted from the last real interaction reported through `POST /auth/activity`. Polling and the live-event stream don't count, so an unattended screen still times out. Admin sessions also end 12 hours after sign-in whatever the activity. Tokens and cookies last until that absolute end plus one hour (viewers: a 7-day backstop plus one hour); the session row in PostgreSQL, not the token, decides. A user can call `/auth/activity` from a script to stay signed in; that is accepted, since they could equally keep clicking. The global `session_timeout` setting is no longer used.

### B. Distributed Multi-Worker Session Governance
In production, `ephemres-api` runs with multiple Uvicorn workers (`--workers 2`). Ephemres prevents single-worker auth bypasses via distributed session drivers:
* **Driver Architecture:** Session validity is verified on every request against `PostgresSessionStore`, which all workers share, in both Standard and Performance Mode. The store is the only authority: a worker never accepts a session from its own memory, and a store error rejects the request (fail closed).
* **Authorization From the Database:** The role and the active / must-change flags are read from the account's database row on every request; the token's `role` claim is ignored. Demoting, promoting or disabling an account takes effect on its next request, and a role change also signs the account out.
* **Concurrent Device Quota:** Each account has its own session limit (`users.max_sessions`, 1–10); `[security] max_active_sessions_per_user` (default 2) is the value new accounts start with. A further login evicts the oldest session (FIFO), and that browser is told it was signed in elsewhere.
* **Session List for Admins:** Admins can list an account's live sessions (address and browser recorded at sign-in) and end one or all of them; both are audited. The token id never leaves the server, and an admin's own current session can't be ended there.
* **Why a Session Ended:** Each ended session leaves a row in `ended_sessions` (reason, kept 8 days), and the `401` for its token carries `X-Session-End-Reason`, so users see "signed in on another device" instead of an unexplained sign-out.
* **Instantaneous Session Revocation:** Changing an account password, an admin reset, disabling or re-roling an account, or clicking "Sign Out Everywhere" deletes its session records from the shared store, so the next request on any worker is rejected.
* **Forced Initial Password Reset (API-enforced):** Accounts flagged `must_change_password` (new users, admin resets, the installer's admin, upgrade resets) get `403 Password change required` on every API route except `POST /api/v1/auth/change-password`, which always requires the current (temporary) password.
* **No Default Admin Password:** `deploy/install.sh` generates a random initial admin password, prints it once and stores only its scrypt hash. `deploy/upgrade.sh` replaces an `admin` password still equal to an earlier installer default (`admin`, or the old env file's `DEFAULT_ADMIN_PASSWORD`) with a random temporary one in the root-only `/opt/ephemres/admin_password_reset.txt`, and removes `DEFAULT_ADMIN_PASSWORD` from the env files.

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
* **Threshold:** After `lockout_threshold` failed attempts (default 5; **Admin Settings → Users & access → Sign-in protection**) the `IP:username` pair is locked for 15 minutes. The failure counter resets after 30 minutes without further failures.
* **Shared, Persistent Counters:** Failures are counted in the store all workers share (the `auth_login_failures` table, in both modes), with one atomic upsert per failure. Counters survive API restarts and cannot be pushed out by flooding sign-ins with other usernames.
* **No Username Oracle:** A sign-in for an unknown or disabled username spends the same scrypt verification as a real one, so response time does not reveal which usernames exist. Concurrent scrypt work is capped per worker (128 MiB each).
* **Edge Rate Limit:** The shipped Nginx site limits `POST /api/v1/auth/login` to 10 requests a minute per client address (burst 5) and answers `429` beyond that. Existing sites are not rewritten by upgrades: copy the `limit_req_zone` line and the `location = /api/v1/auth/login` block from `deploy/nginx.conf.template`.
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
* The only elevated action available to the service user is a sudoers rule (`/etc/sudoers.d/ephemres`, mode `0440`) allowing `systemctl start|stop|restart|is-active|enable|disable ephemres-flowd`, used when an administrator toggles flow ingestion in the Settings UI.

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
- [ ] **Database & IPC Isolation:** Ensure PostgreSQL (`5432`) listens strictly on `127.0.0.1` and FastStore shared-memory files (`/run/ephemres/ephemres_faststore*`, `/dev/shm` fallback) are owned by `ephemres:ephemres` (`0600`).
- [ ] **Admin Password Changed:** Sign in with the initial password the installer printed (or the one in `/opt/ephemres/admin_password_reset.txt` after an upgrade), set your own, and delete that file.
- [ ] **Secrets Present:** `/etc/ephemres/ephemres.env` exists and holds `EPHEMRES_SECRET_KEY` (32+ characters). The services refuse to start without it rather than fall back to the public development key.
- [ ] **Firewall Egress Rules:** Restrict outbound egress from the Ephemres server strictly to necessary destinations (Port 443 for webhooks, Port 587 for SMTP).

---

## 12. Vulnerability Reporting & Coordinated Disclosure

If you discover a security vulnerability or potential threat in Ephemres, please disclose it responsibly:

* **Security Contact:** `dagera07@gmail.com` or via **[GitHub Private Vulnerability Reporting](https://github.com/dc8official/ephemres/security/advisories/new)** on the repository (`https://github.com/dc8official/ephemres`).
* **Policy:** Please do not file public GitHub issues for security vulnerabilities.
* **Response SLA:** The Ephemres security team acknowledges vulnerability reports within **24 hours** and aims to deliver patches within **72 hours** for high or critical severity findings.
