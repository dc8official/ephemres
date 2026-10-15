# Ephemres Security Model, Threat Architecture & Defense Specification

**Document Version:** 3.3.1n  
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
| **v3.3.1n** | Migration & Rebranding Release | **Active / Current Standard** | Production standard with Ephemres rebrand and flattened `/opt/ephemres` tree. |
| **v3.3.0** | Feature & Ingestion Release | **Frozen Legacy Snapshot** | Frozen snapshot with Flow Exporter & Interface Telemetry. |
| **v3.1.35s** | Maintenance Release | **Frozen Legacy Snapshot** | Frozen long-term maintenance track. |
| **<= v3.2.0** | Legacy Release | **Superseded** | Upgrade to v3.3.1n recommended. |
| **< v3.1** | Legacy Alpha / Beta | **End of Life (EOL)** | Unmaintained; migrate to v3.3.1n immediately. |

---

## 3. Network & Firewall Perimeter Architecture

```
                                      PUBLIC INTERNET / LAN
                                                │
                                                ▼
                                   ┌─────────────────────────┐
                                   │   Nginx Reverse Proxy   │ (Port 80 / 443)
                                   │ SSL/TLS 1.3 Termination │
                                   └────────────┬────────────┘
                                                │
                       ┌────────────────────────┼────────────────────────┐
                       │ (Port 8000)            │ (UDP 2055 / 4739)      │
                       ▼                        ▼                        ▼
              ┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
              │   FastAPI Core   │    │ Flow Collector   │    │  Traceroute/ICMP │
              │  (ephemres-api)  │    │ (ephemres-flowd) │    │(ephemres-engine) │
              └────────┬─────────┘    └────────┬─────────┘    └────────┬─────────┘
                       │                       │                       │
                       └───────────────────────┼───────────────────────┘
                                               │
                                               ▼
                              ┌──────────────────────────────────┐
                              │  Internal Loopback Services      │
                              │  PostgreSQL (5432) / Redis (6379)│
                              └──────────────────────────────────┘
```

| Service Component | Port(s) | Binding Scope | Security Hardening Controls |
| :--- | :--- | :--- | :--- |
| **Nginx Web Server** | `80` / `443` | `0.0.0.0` (Public / LAN) | Public ingress point. Enforces TLS 1.2+, HSTS, Content Security Policy, and proxies internal API traffic. |
| **FastAPI Backend (`ephemres-api`)** | `8000` | `127.0.0.1` (Loopback Only) | Internal application server. Blocked from direct external access by firewall. |
| **Flow Collector (`ephemres-flowd`)** | `2055` / `4739` | UDP Management Ingress | Autonomous flow collector. 4MB kernel receive buffer, zero-allocation binary parsing, memory circuit breaker. |
| **PostgreSQL / TimescaleDB** | `5432` | `127.0.0.1` (Loopback Only) | Internal database. Protected by strong randomized password; strictly loopback bound. |
| **Redis Cache / Broker (Optional)** | `6379` | `127.0.0.1` (Loopback Only) | Internal session, stream buffer & pub/sub broker. Bound to loopback with `protected-mode yes`. |
| **ICMP Ping Probes** | ICMP | Outbound | Low-frequency synthetic health probes (default 32s budget). |
| **Outbound Webhooks / Email** | `443` / `587` | Outbound Egress | Outbound notifications to Teams, Discord, Slack, and SMTP gateways. Protected by `SSRFSafeBackend`. |

---

## 4. Outbound Egress & Zero-Trust SSRF Protection (CWE-918)

Ephemres incorporates **Zero-Trust Socket-Level SSRF Protection** (`SSRFSafeBackend`) and **Pre-Resolved Socket DNS Pinning** (`synthetic.py`), replacing traditional pre-flight hostname checks with kernel-boundary connection verification.

### A. The Threat: Time-of-Check to Time-of-Use (TOCTOU) DNS Rebinding
In standard application architectures, validating a URL before making an HTTP request leaves an exploitable window: an attacker configures a domain with a 0-second TTL that returns a public IP during pre-flight validation, but rebinds to `169.254.169.254` (cloud metadata) or `127.0.0.1` when the HTTP client establishes its TCP connection.

### B. The Defense: Millisecond-Level Socket Interception & Socket DNS Pinning
Ephemres's `SSRFSafeBackend` wraps the low-level `httpcore.AsyncNetworkBackend` engine, while `monitoring/synthetic.py` implements direct socket DNS pinning:

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
* **Key Derivation:** Master keys are derived using **HKDF-SHA256** (RFC 5869) with a domain-separated salt (`ephemres-crypto-v1`).
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
* **Sliding Inactivity Window:** User sessions expire after 120 minutes of inactivity, sliding forward on active requests.

### B. Distributed Multi-Worker Session Governance
In production, `ephemres-api` runs with multiple Uvicorn workers (`--workers 2`). Ephemres prevents single-worker auth bypasses via distributed session drivers:
* **Driver Architecture:** Session validity is verified asynchronously against `PostgresSessionStore` or `RedisSessionStore` rather than volatile single-worker process memory.
* **Concurrent Device Quota (Max 2 Sessions):** Accounts are restricted to 2 concurrent active devices. A third login automatically evicts the oldest session via FIFO retirement.
* **Instantaneous Session Revocation:** Changing an account password or clicking "Sign Out Everywhere" immediately deletes active session records from the datastore, revoking access across all workers in real time.
* **Forced Initial Password Reset:** Newly provisioned user accounts (`ADMIN` or `VIEWER`) must change their temporary password upon first login before accessing system telemetry.

### C. Password Hashing
* Passwords are hashed using **Argon2id** (memory-hard, resistant to GPU/ASIC cracking) with fallback support for **bcrypt** (cost factor 12).
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
* **Threshold:** 5 consecutive failed login attempts within 15 minutes trigger a temporary 15-minute account lockout.
* **NAT / VPN Isolation:** Lockout keys are scoped by `f"{client_ip}:{username}"`. If an external attacker attempts to brute force an administrator account, only the attacker's IP is locked out; legitimate administrators on corporate networks remain unaffected.

---

## 8. Input Sanitization & Attack Mitigations

### A. SQL Injection Neutralization
* 100% of database interactions execute through **SQLAlchemy Core / ORM parameterized queries** (`:endpoint_id`, `:ip_address`) or explicit type-casted bindings (`CAST(:id AS uuid)`).
* Raw string concatenation into SQL statements is strictly prohibited.

### B. CSV Formula Injection Defense (Spreadsheet Execution)
* Telemetry exports stream large datasets that operators open in Microsoft Excel or LibreOffice.
* **Formula Defense:** In `backend/app/routers/reports.py`, any cell value starting with `=`, `+`, `-`, `@`, `\t`, or `\r` is automatically prefixed with a single quote (`'`), neutralizing spreadsheet formula execution.

### C. Notification Payload Sanitization
* **HTML Injection in Email:** In `backend/app/services/alert_formatters.py`, all dynamic endpoint parameters (`endpoint_name`, `ip_address`, `event_type`) are sanitized with `html.escape()`.
* **CRLF Email Header Injection:** All email `Subject`, `To`, and `From` headers are sanitized to strip carriage returns (`\r`) and line feeds (`\n`), preventing Bcc injection or mail gateway manipulation.
* **Discord Mention Storms:** Discord webhooks enforce `"allowed_mentions": {"parse": []}`, preventing malicious hostnames (e.g. `@everyone` or `@here`) from triggering server-wide notification storms.
* **Slack MRKDWN Escaping:** Dynamic strings in Slack Block Kit payloads escape special formatting characters (`<`, `>`, `&`).

### D. Air-Gapped PDF Generation & SSRF/LFI Defense
* **Risk Model:** PDF rendering engines that support HTML/CSS are historically susceptible to Server-Side Request Forgery (SSRF) and Local File Inclusion (LFI) through tags like `<img src="...">`, `<link>`, or `@import`.
* **Air-Gapped URL Fetcher:** In `backend/app/services/pdf_engine.py`, the `secure_url_fetcher` intercepts all resource loading attempts:
  * **External Schemas Blocked:** All network URI schemes (`http://`, `https://`, `ftp://`) raise an immediate `PermissionError`, preventing remote requests or credential exfiltration.
  * **Filesystem Whitelisting:** Local `file://` URIs are verified to reside strictly within authorized static asset roots (`backend/app/templates/reports/` and `frontend/src/assets/`). Traversal attempts outside these paths (e.g., `/etc/passwd`) are blocked with a `PermissionError`.
  * **Safe Data URIs:** Permits inline Base64 data URIs (`data:image/png;base64,...`) for corporate branding logos.

### E. Cryptographic Document Immutability & Permission Encryption
* **Read-Only Permission Encryption:** In `backend/app/services/pdf_engine.py`, rendered PDFs are processed through `pypdf.PdfWriter` applying standard permission encryption with an empty user password and randomized 256-bit owner password.
* **Tamper Prevention:** Permissions are locked strictly to `UserAccessPermissions.PRINT | UserAccessPermissions.PRINT_TO_REPRESENTATION`. Annotation editing, form filling, text modifications, and content extraction are prohibited by standard PDF viewers.
* **Out-of-Band Integrity Checksum:** The cryptographic SHA-256 hash of the generated document is transmitted via the `X-Report-SHA256` HTTP header, allowing verification against audit logs without cluttering the printed visual page.

---

## 9. Operating System & Privilege Hardening

### A. Non-Root Daemon Execution
* Both `ephemres-api` and `ephemres-engine` run under the dedicated unprivileged system user `ephemres` (UID/GID isolated).
* Daemons do not possess `sudo` or root privileges.

### B. Linux Capability Isolation (`CAP_NET_RAW`)
* Raw socket creation for ICMP echo requests (ping) typically requires root privileges.
* Instead of running daemons as root or marking Python setuid-root, Ephemres isolates the capability strictly to the Python interpreter using Linux file capabilities:
  ```bash
  setcap cap_net_raw+ep $(readlink -f /opt/ephemres/venv/bin/python3)
  ```
* This grants packet crafting rights strictly for network diagnostics without granting file system, memory, or administrative OS rights.

### C. Systemd Sandboxing Directives
The production unit files (`deploy/ephemres-api.service` and `deploy/ephemres-engine.service`) enforce system-level isolation:
* `ProtectSystem=full`: Mounts `/usr`, `/boot`, and `/etc` read-only for daemon processes.
* `PrivateTmp=true`: Allocates isolated, private `/tmp` directories inaccessible to other system users.
* `NoNewPrivileges=true`: Prevents child processes from gaining elevated privileges via setuid binaries.

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

- [ ] **HTTPS Enforced:** Nginx configured with valid SSL/TLS certificates and HSTS enabled.
- [ ] **File Permissions:** Verify `/etc/ephemres/ephemres.env` has permissions `0600` owned by `ephemres:ephemres`.
- [ ] **Database Network Isolation:** Ensure PostgreSQL (`5432`) and Redis (`6379`) listen strictly on `127.0.0.1`.
- [ ] **Admin Password Changed:** Verify the default administrator password was changed upon initial login.
- [ ] **Firewall Egress Rules:** Restrict outbound egress from the Ephemres server strictly to necessary destinations (Port 443 for webhooks, Port 587 for SMTP).

---

## 12. Vulnerability Reporting & Coordinated Disclosure

If you discover a security vulnerability or potential threat in Ephemres, please disclose it responsibly:

* **Security Contact:** `security@ephemres.internal` or via **GitHub Private Vulnerability Reporting** on the repository (`https://github.com/dc8official/ephemres`).
* **Policy:** Please do not file public GitHub issues for security vulnerabilities.
* **Response SLA:** The Ephemres security team acknowledges vulnerability reports within **24 hours** and aims to deliver patches within **72 hours** for high or critical severity findings.
