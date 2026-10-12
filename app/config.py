# Standard imports.
from pathlib import Path

# ========== App Config ==========
HOST = "127.0.0.1"
PORT = 5000

# ========== Object Config ==========
MANAGED_BY_LABEL: str = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE: str = "test-run"

# ========== Quota Config ==========
QUOTA_CONFIG_PATH: Path = Path(__file__).resolve().parent.parent / "quota.yaml"
QUOTA_NAME:        str  = "deployment-quota"