"""Run every test against the example config, in a throwaway working directory,
so nothing touches real data and CI needs no secrets."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["BOT_CONFIG"] = os.path.join(ROOT, "Config.example.json")
sys.path.insert(0, ROOT)
os.chdir(tempfile.mkdtemp(prefix="barnabus-test-"))

from barnabus import core  # noqa: E402

core.config["DatabasePath"] = ":memory:"
core.config["RecordKeyPath"] = os.path.join(os.getcwd(), "record.key")
