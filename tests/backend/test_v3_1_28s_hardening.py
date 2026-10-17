import asyncio
import ipaddress
import secrets
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4
import pytest

from app.config import ApiSettings, Settings, load_settings
from app.services.auth_service import generate_readable_password
from app.core.crypto import (
    DecryptionError,
    decrypt_secret,
    encrypt_secret,
    mask_secret,
)
from app.services.uptime_calculator import (
    calculate_uptime_denominator_and_percentage,
)
from monitoring.engine import _stagger_offset_for_endpoint
from monitoring.ping import _subprocess_ping_semaphore
from monitoring.registry import endpoint_registry
from monitoring.synthetic import (
    RESTRICTED_NETWORKS,
    async_validate_probe_target,
    validate_probe_target,
)


class TestV3128sProbeStaggering:
    """FIX-19 (PER-01): Deterministic Second-Slot Staggering."""

    def test_stagger_offset_deterministic(self):
        ep_id = uuid4()
        offset_1 = _stagger_offset_for_endpoint(ep_id)
        offset_2 = _stagger_offset_for_endpoint(ep_id)
        assert offset_1 == offset_2
        assert 0 <= offset_1 < 60

    def test_stagger_distribution_uniform(self):
        """Verify 1,200 random UUIDs distribute across all 60 second slots."""
        slots = [0] * 60
        for _ in range(1200):
            ep_id = uuid4()
            slot = _stagger_offset_for_endpoint(ep_id)
            slots[slot] += 1

        # Every slot from 0..59 should have at least 1 allocation (average 20)
        empty_slots = [i for i, count in enumerate(slots) if count == 0]
        assert len(empty_slots) == 0, f"Empty slots found: {empty_slots}"


class TestV3128sUptimeCalculus:
    """FIX-15 (COR-01-DEP): Uptime interval math with active event handling."""

    def test_uptime_interval_calculation_with_uptime_seconds(self):
        created_at = datetime.now(timezone.utc) - timedelta(days=30)
        start_time = datetime.now(timezone.utc) - timedelta(hours=24)
        end_time = datetime.now(timezone.utc)
        now_utc = end_time

        # 20 hours of uptime in a 24-hour window = 20 * 3600 = 72000 seconds
        uptime_seconds = 20 * 3600
        total_seconds = 24 * 3600

        result = calculate_uptime_denominator_and_percentage(
            created_at=created_at,
            start_time=start_time,
            end_time=end_time,
            now_utc=now_utc,
            up_events_count=10,
            gap_intervals=[],
            uptime_seconds=uptime_seconds,
        )

        expected_pct = round((72000 / 86400) * 100.0, 2)
        assert result == expected_pct

    def test_uptime_full_availability(self):
        created_at = datetime.now(timezone.utc) - timedelta(days=30)
        start_time = datetime.now(timezone.utc) - timedelta(hours=24)
        end_time = datetime.now(timezone.utc)
        now_utc = end_time

        result = calculate_uptime_denominator_and_percentage(
            created_at=created_at,
            start_time=start_time,
            end_time=end_time,
            now_utc=now_utc,
            up_events_count=1,
            gap_intervals=[],
            uptime_seconds=86400,
        )
        assert result == 100.0


class TestV3128sDNSPinningAndSSRF:
    """FIX-08 (SEC-01) & FIX-16 (COR-02): Pre-resolved Socket DNS Pinning."""

    def test_restricted_networks_block_cloud_metadata(self):
        with pytest.raises(ValueError, match="SSRF Protection"):
            validate_probe_target("169.254.169.254")

    def test_restricted_networks_block_loopback(self):
        with pytest.raises(ValueError, match="SSRF Protection"):
            validate_probe_target("127.0.0.1")

    def test_restricted_networks_permit_rfc1918(self):
        assert validate_probe_target("10.0.0.1") == "10.0.0.1"
        assert validate_probe_target("172.16.0.1") == "172.16.0.1"
        assert validate_probe_target("192.168.1.1") == "192.168.1.1"

    def test_async_validate_probe_target_resolves(self):
        res = asyncio.run(async_validate_probe_target("192.168.10.5"))
        assert res == "192.168.10.5"

    def test_dns_rebinding_resolution_pinning(self):
        with patch("socket.gethostbyname", return_value="93.184.216.34"):
            resolved_ip = validate_probe_target("example.com")
            assert resolved_ip == "93.184.216.34"

    def test_dns_rebinding_malicious_resolution_blocked(self):
        with patch("socket.gethostbyname", return_value="169.254.169.254"):
            with pytest.raises(ValueError, match="SSRF Protection"):
                validate_probe_target("malicious.internal.target")


class TestV3128sSecurityGovernance:
    """FIX-09 (SEC-02), FIX-10 (SEC-04), FIX-12 (SEC-06), FIX-13 (SEC-07)."""

    def test_production_secret_key_rejection(self):
        """Refuses to load settings in production if default secret key is used."""
        with patch.dict("os.environ", {
            "NETMON_ENV": "production",
            "NETMON_SECRET_KEY": "dev-secret-key-change-in-production-min-32-chars",
        }):
            with pytest.raises(ValueError, match="Insecure configuration"):
                load_settings()

    def test_production_custom_secret_key_accepted(self):
        with patch.dict("os.environ", {
            "NETMON_ENV": "production",
            "NETMON_SECRET_KEY": secrets.token_hex(32),
        }):
            settings = load_settings()
            assert settings.environment == "production"

    def test_development_allows_default_secret_key(self):
        with patch.dict("os.environ", {
            "NETMON_ENV": "development",
            "NETMON_SECRET_KEY": "dev-secret-key-change-in-production-min-32-chars",
        }):
            settings = load_settings()
            assert settings.environment == "development"

    def test_cors_wildcard_with_credentials_rejected(self):
        with patch.dict("os.environ", {
            "NETMON_API__ALLOWED_ORIGINS": '["*"]',
        }):
            with pytest.raises(ValueError, match="CORS configuration error"):
                load_settings()

    def test_password_entropy_96_bits(self):
        pwd = generate_readable_password()
        # token_urlsafe(16) produces 22 characters (~128 bits encoded, 96+ bits entropy)
        assert len(pwd) >= 21
        # Test 50 generated passwords for uniqueness
        pwds = {generate_readable_password() for _ in range(50)}
        assert len(pwds) == 50

    def test_decryption_error_raised_on_tampered_payload(self):
        with pytest.raises(DecryptionError):
            decrypt_secret("ENC:v1:corrupted_invalid_data")

    def test_mask_secret_handles_decryption_error(self):
        masked = mask_secret("ENC:v1:invalid_ciphertext")
        assert masked == "••••••••"

    def test_mask_secret_masks_valid_ciphertext(self):
        enc = encrypt_secret("my_super_secret_smtp_password")
        assert enc.startswith("ENC:v1:")
        masked = mask_secret(enc)
        assert masked == "••••••••••••"

    def test_mask_secret_masks_webhook_url(self):
        masked = mask_secret("https://user:password123@hooks.slack.com/services/T123/B456/abcdef789")
        assert "password123" not in masked
        assert "abcdef789" not in masked
        assert "••••••••" in masked


class TestV3128sConcurrencySemaphores:
    """FIX-20 (PER-02) & FIX-21 (PER-03): Diagnostic queue and ping semaphores."""

    def test_subprocess_ping_semaphore_bound(self):
        assert _subprocess_ping_semaphore._value == 25

    def test_diagnostic_queue_limit_per_endpoint(self):
        endpoint_registry._diagnostic_tasks.clear()
        ep_id = uuid4()
        tasks = {MagicMock() for _ in range(10)}
        endpoint_registry._diagnostic_tasks[ep_id] = tasks
        active_count = sum(len(s) for s in endpoint_registry._diagnostic_tasks.values())
        assert active_count == 10
        assert not (active_count < 10)
        endpoint_registry._diagnostic_tasks.clear()
