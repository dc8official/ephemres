import os
import re
import unittest
from pathlib import Path

class TestSystemIntegrityAndFixes(unittest.TestCase):
    """
    Validation test suite ensuring system integrity across frontend error parsing,
    SQL UUID parameter casting in API routers, and deployment/upgrade script correctness.
    """

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[2]

    def test_dashboard_view_error_resolution_logic(self) -> None:
        """
        Verify DashboardView.vue checks err.response.data.detail before falling back.
        """
        dashboard_path = self.repo_root / "frontend" / "src" / "views" / "DashboardView.vue"
        self.assertTrue(dashboard_path.exists(), "DashboardView.vue must exist")

        content = dashboard_path.read_text(encoding="utf-8")
        self.assertIn(
            "err.response?.data?.detail",
            content,
            "DashboardView.vue catch block must inspect err.response?.data?.detail for error messages."
        )

    def test_upgrade_script_execution_order_and_venv_detection(self) -> None:
        """
        Verify upgrade.sh builds frontend before rsync sync, supports /opt/netmon/venv,
        and exports credentials for Alembic migrations.
        """
        upgrade_path = self.repo_root / "deploy" / "upgrade.sh"
        self.assertTrue(upgrade_path.exists(), "deploy/upgrade.sh must exist")

        content = upgrade_path.read_text(encoding="utf-8")

        # 1. Frontend build before rsync
        npm_build_idx = content.find("npm run build")
        rsync_idx = content.find("rsync -a --delete")
        self.assertNotEqual(npm_build_idx, -1, "upgrade.sh must execute npm run build")
        self.assertNotEqual(rsync_idx, -1, "upgrade.sh must execute rsync to production target")
        self.assertLess(
            npm_build_idx,
            rsync_idx,
            "Frontend build must execute BEFORE rsync syncs compiled assets to production directory."
        )

        # 2. Virtual environment detection
        self.assertTrue("/opt/ephemres/venv" in content or "/opt/netmon/venv" in content, "upgrade.sh must check virtualenv path")

        # 3. Environment variable exports for Alembic migrations
        self.assertTrue("EPHEMRES_DB_PASSWORD" in content or "NETMON_DB_PASSWORD" in content, "upgrade.sh must export DB_PASSWORD for Alembic migrations")

    def test_api_routers_sql_uuid_casting(self) -> None:
        """
        Verify that SQL queries in endpoints.py, reports.py, auth.py, and users.py cast UUID parameters.
        """
        routers_dir = self.repo_root / "backend" / "app" / "routers"
        target_files = ["endpoints.py", "reports.py", "auth.py", "users.py"]

        for filename in target_files:
            filepath = routers_dir / filename
            self.assertTrue(filepath.exists(), f"Router file {filename} must exist")
            content = filepath.read_text(encoding="utf-8")

            # Check that text(...) query patterns matching WHERE or VALUES with UUID parameters use CAST(... AS uuid)
            uncast_id_pattern = re.compile(r"WHERE\s+[\w\.]*id\s*=\s*:(endpoint_id|user_id|id)\b(?!(\s*AS\s*uuid|::uuid))", re.IGNORECASE)
            matches = uncast_id_pattern.findall(content)
            self.assertEqual(
                len(matches),
                0,
                f"Un-cast UUID parameter bindings found in {filename}: {matches}"
            )


if __name__ == "__main__":
    unittest.main()
