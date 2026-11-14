"""The Ephemres release, in one place (v3.3.6).

Until v3.3.6 the version was written out in about twenty places (API, PDF, alert footers, the
UI's fallback), and the v3.3.6 branch still said 3.3.5 everywhere when it was first upgrade-tested
(2026-11-14). Everything that names the release reads it from here; deploy/upgrade.sh reads it
too, from the code it is about to install, to refuse a downgrade. The frontend takes its own copy
from frontend/package.json: bump both together.
"""

VERSION = "3.3.6"
