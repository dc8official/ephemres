# Security Policy

## Supported Versions

| Version | Supported | Security Notes |
| :--- | :--- | :--- |
| **v3.3.2.v** | :white_check_mark: | Active production standard (Vertical Integration Architecture & 13 zero-dependency core engines) |
| **v3.3.0** | :white_check_mark: | Supported frozen snapshot (Flow Exporter & Interface Telemetry) |
| **v3.1.35s** | :white_check_mark: | Supported frozen snapshot (Enterprise LTS maintenance track) |
| **v3.3.1n** | :x: | Transitional rebranding release — superseded by `v3.3.2.v` |
| **<= v3.2.0** | :x: | Superseded — upgrade to `v3.3.2.v` recommended |
| **< v3.1** | :x: | End of Life (EOL) |

## Reporting a Vulnerability

The Ephemres engineering and security team takes the security of our network monitoring platform seriously. If you discover a vulnerability, we appreciate your responsible disclosure.

### How to Report

- **Email:** Send details to `dagera07@gmail.com`
- **GitHub:** Use [Private Vulnerability Reporting](https://github.com/dc8official/ephemres/security/advisories/new) on the repository.

Please include:
1. Description of the vulnerability and its potential impact.
2. Step-by-step instructions or proof-of-concept to reproduce the issue.
3. Affected components and environment details.

Please **do not** report security vulnerabilities through public GitHub issues.

### Response Timeline

- **Initial Response:** Within 24 hours
- **Triage & Status Update:** Within 48 hours
- **Remediation & Patch Target:** Within 72 hours for critical/high vulnerabilities

## Security Architecture & Threat Model

For the full technical security specification, threat models, cryptographic algorithms (`hashlib.scrypt` password hashing, `AES-256-GCM` secret encryption via `HKDF-SHA256`), socket-level SSRF defenses, perimeter port policies, and Linux capability isolation (`CAP_NET_RAW`), see the comprehensive [Ephemres Security Model & Defense Specification](docs/security.md).
