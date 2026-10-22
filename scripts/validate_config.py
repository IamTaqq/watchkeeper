"""Validate checks.yaml with the same rules the checker uses at runtime."""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from watchkeeper.config import ConfigError, parse_checks  # noqa: E402

DEMO_NAMES = {"demo-app-health", "demo-data-freshness", "demo-cron"}

path = Path(sys.argv[1] if len(sys.argv) > 1 else "checks.yaml")
try:
    checks = parse_checks(yaml.safe_load(path.read_text()))
except (ConfigError, yaml.YAMLError) as exc:
    print(f"{path}: {exc}")
    sys.exit(1)

clash = DEMO_NAMES & {c.name for c in checks}
if clash:
    print(f"{path}: {sorted(clash)} are reserved for the demo checks; rename them")
    sys.exit(1)

print(f"{path}: {len(checks)} check(s) OK")
for c in checks:
    print(f"  - {c.name} ({c.type}, alert after {c.failure_threshold} failed run(s))")
