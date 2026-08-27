"""Environment for the whole suite.

Several modules read configuration at import time, so whichever test file
imported first used to decide how the orchestrator was configured for every
other one. Adding a test that imported it earlier in alphabetical order was
enough to disable the write tools and fail nine unrelated tests.

pytest loads conftest before any test module, so settings belong here rather
than at the top of whichever file happened to need them first.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for package in ("orchestrator", "vcfLogs", "vcfNetworks", "veeam"):
    sys.path.insert(0, os.path.join(ROOT, package))

os.environ.setdefault("MCP_SERVER", "http://fake")
os.environ.setdefault("OLLAMA_URL", "http://fake")
os.environ.setdefault("ENABLE_WRITE_TOOLS", "true")
os.environ.setdefault("WRITE_REQUIRE_CONFIRM", "true")
os.environ.setdefault("AUDIT_LOG", os.path.join(tempfile.mkdtemp(), "audit.log"))

os.environ.setdefault("LOGS_URL", "https://fake:9543")
os.environ.setdefault("LOGS_PASSWORD", "p")
os.environ.setdefault("NI_HOST", "fake")
os.environ.setdefault("NI_USERNAME", "u")
os.environ.setdefault("NI_PASSWORD", "p")
os.environ.setdefault("VEEAM_URL", "https://fake:9419")
os.environ.setdefault("VEEAM_USER", "u")
os.environ.setdefault("VEEAM_PASSWORD", "p")
