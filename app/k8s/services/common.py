# Standard imports.
import logging
from typing import (
    Any,
    Dict,
    Optional,
)

# Third-party imports.
import yaml
from kubernetes import client
from kubernetes.client.rest import ApiException

# Local imports.
from app.k8s.api import init_core_api
from app.config import (
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    QUOTA_CONFIG_PATH,
    QUOTA_NAME,
)
from app.schema.quota import QuotaConfig

logger = logging.getLogger(__name__)

def _load_quota() -> Optional[QuotaConfig]:
    # Read once at startup, so an invalid quota.yaml stops the app here instead of failing a later create.
    # Without the file, new namespaces get no quota.
    try:
        with open(QUOTA_CONFIG_PATH) as f:
            return QuotaConfig.model_validate(yaml.safe_load(f))
    except FileNotFoundError:
        logger.warning("%s not found: namespaces are created without a ResourceQuota.", QUOTA_CONFIG_PATH)
        return None

_QUOTA: Optional[QuotaConfig] = _load_quota()

def _build_quota(quota: QuotaConfig) -> Dict[str, Any]:
    # Caps the namespace's total CPU and memory requests (what the scheduler reserves), not limits.
    # https://kubernetes.io/docs/concepts/policy/resource-quotas/
    return {
        "apiVersion": "v1",
        "kind":       "ResourceQuota",
        "metadata": {
            "name":   QUOTA_NAME,
            "labels": {MANAGED_BY_LABEL: MANAGED_BY_VALUE},
        },
        "spec": {
            "hard": {
                "requests.cpu":    quota.requests.cpu,
                "requests.memory": quota.requests.memory,
            },
        },
    }

def namespace_exists(namespace: str) -> bool:
    try:
        core_api: client.CoreV1Api = init_core_api()
        core_api.read_namespace(namespace)
        return True # No error raised, namespace exists.
    except ApiException as e:
        if (e.status == 404):
            return False
        else:
            raise

def create_namespace(namespace: str) -> None:
    core_api: client.CoreV1Api = init_core_api()
    core_api.create_namespace(
        client.V1Namespace(
            metadata = client.V1ObjectMeta(
                name = namespace,
            )
        )
    )

    # Every namespace the API creates gets the quota from quota.yaml.
    if (_QUOTA is None):
        return
    try:
        core_api.create_namespaced_resource_quota(
            namespace = namespace,
            body      = _build_quota(_QUOTA),
        )
    except Exception:
        # The quota is only added here, so a namespace left without one would stay unlimited: undo it.
        core_api.delete_namespace(namespace)
        raise
