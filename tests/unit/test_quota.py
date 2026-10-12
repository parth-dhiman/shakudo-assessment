# Standard imports.
import json
from decimal import Decimal
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Set,
)

# Third-party imports.
import pytest
import yaml
from flask import Response
from kubernetes import client
from pydantic import ValidationError

# Local imports.
from app.api import app
from app.config import (
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    QUOTA_CONFIG_PATH,
    QUOTA_NAME,
)
from app.exceptions import QuotaExceededError
from app.k8s.services.common import (
    _QUOTA,
    _build_quota,
)
from app.k8s.services.deployments import (
    _check_quota,
    _format_quantity,
)
from app.schema.quota import QuotaConfig

def _error_fields(exc_info) -> Set[str]:
    return {
        ".".join(str(p) for p in e["loc"])
        for e in exc_info.value.errors()
    }

# ==================== quota.yaml ====================

def test_repository_quota_file_is_loaded():
    # The file in the repository is what every new namespace gets, so it must stay valid.
    with open(QUOTA_CONFIG_PATH) as f:
        expected: QuotaConfig = QuotaConfig.model_validate(yaml.safe_load(f))
    assert (_QUOTA == expected)

@pytest.mark.parametrize(
    "config",
    [
        {"requests": {"cpu": "1", "memory": "2Gi"}},
        {"requests": {"cpu": "500m", "memory": "512Mi"}},
        {"requests": {"cpu": "0", "memory": "0"}},          # a zero quota is valid: it blocks every deployment
    ],
    ids = ["whole-core", "millicores", "zero"],
)
def test_valid_quota_configs(config: Dict[str, Any]):
    quota: QuotaConfig = QuotaConfig.model_validate(config)
    assert (quota.requests.cpu    == config["requests"]["cpu"])
    assert (quota.requests.memory == config["requests"]["memory"])

@pytest.mark.parametrize(
    ("config", "fields"),
    [
        (None,                                                     {""}),
        ({},                                                       {"requests"}),
        ({"requests": {"cpu": "1"}},                               {"requests.memory"}),
        ({"requests": {"memory": "2Gi"}},                          {"requests.cpu"}),
        ({"requests": {"cpu": 1, "memory": "2Gi"}},                {"requests.cpu"}),     # unquoted YAML number
        ({"requests": {"cpu": "lots", "memory": "2Gi"}},           {"requests.cpu"}),
        ({"requests": {"cpu": "-1", "memory": "2Gi"}},             {"requests.cpu"}),
        ({"requests": {"cpu": "1", "memory": "2Gi", "gpu": "1"}},  {"requests.gpu"}),
        ({"requests": {"cpu": "1", "memory": "2Gi"}, "limits": {}}, {"limits"}),          # only requests are capped
    ],
    ids = [
        "empty-file", "no-requests", "cpu-only", "memory-only", "unquoted-number",
        "not-a-quantity", "negative", "unknown-resource", "limits-section",
    ],
)
def test_invalid_quota_configs(config: Any, fields: Set[str]):
    with pytest.raises(ValidationError) as exc_info:
        QuotaConfig.model_validate(config)
    assert (_error_fields(exc_info) == fields)

# ==================== ResourceQuota ====================

def test_build_quota():
    quota: QuotaConfig = QuotaConfig.model_validate({"requests": {"cpu": "1", "memory": "2Gi"}})
    assert (_build_quota(quota) == {
        "apiVersion": "v1",
        "kind":       "ResourceQuota",
        "metadata": {
            "name":   QUOTA_NAME,
            "labels": {MANAGED_BY_LABEL: MANAGED_BY_VALUE},
        },
        "spec": {
            "hard": {
                "requests.cpu":    "1",
                "requests.memory": "2Gi",
            },
        },
    })

# ==================== Quota check (snapshot) ====================

# Recorded with `kubectl get resourcequota -n <ns> -o json` with 3 pods each requesting 100m and 128Mi:
#   deployment-quota (ours):  requests.cpu 300m of 1,  requests.memory 384Mi of 2Gi
#   extra (added by hand):    cpu 300m of 500m,  memory 384Mi of 1Gi,  pods 3 of 10   (Kubernetes' short keys)
_SNAPSHOT: Path = Path(__file__).parent.parent / "fixtures" / "quota_in_use.json"
_QUOTAS: List[client.V1ResourceQuota] = [
    client.V1ResourceQuota.from_dict(item) for item in json.loads(_SNAPSHOT.read_text())["items"]
]
_NAMESPACE: str = _QUOTAS[0].metadata.namespace

def _quota(name: str) -> client.V1ResourceQuota:
    return next(quota for quota in _QUOTAS if quota.metadata.name == name)

@pytest.mark.parametrize(
    ("requests", "pods"),
    [
        ({"cpu": "100m", "memory": "128Mi"}, 2),     # cpu 300m + 200m = 500m: exactly extra's limit, which is allowed
        ({"cpu": "100m", "memory": "128Mi"}, 0),
        ({"cpu": "100m"},                    2),     # memory not requested (an older pod template): not checked
        ({},                                 100),
        ({"cpu": "1m", "memory": "1Mi"},     20),    # 23 pods > extra's pods=10: only requests are checked here
    ],
    ids = ["fills-tightest-quota-exactly", "no-pods", "memory-not-requested", "nothing-requested", "pod-count-not-checked"],
)
def test_check_quota_fits(requests: Dict[str, str], pods: int):
    _check_quota(_NAMESPACE, _QUOTAS, requests, pods)

@pytest.mark.parametrize(
    ("quotas", "requests", "pods", "violations"),
    [
        pytest.param(
            _QUOTAS, {"cpu": "100m", "memory": "128Mi"}, 3,
            ["exceeded quota: extra, requested: cpu=300m, used: cpu=300m, limited: cpu=500m"],
            id = "second-quota-is-tighter",
        ),
        pytest.param(
            _QUOTAS, {"cpu": "100m", "memory": "128Mi"}, 8,
            [
                "exceeded quota: deployment-quota, requested: requests.cpu=800m, used: requests.cpu=300m, limited: requests.cpu=1",
                "exceeded quota: extra, requested: cpu=800m,memory=1Gi, used: cpu=300m,memory=384Mi, limited: cpu=500m,memory=1Gi",
            ],
            id = "both-quotas-and-both-resources",
        ),
        pytest.param(
            _QUOTAS, {"cpu": "10m", "memory": "1Gi"}, 2,
            [
                "exceeded quota: deployment-quota, requested: requests.memory=2Gi, used: requests.memory=384Mi, limited: requests.memory=2Gi",
                "exceeded quota: extra, requested: memory=2Gi, used: memory=384Mi, limited: memory=1Gi",
            ],
            id = "memory-only",
        ),
        pytest.param(
            [_quota("deployment-quota")], {"cpu": "100m", "memory": "128Mi"}, 8,
            ["exceeded quota: deployment-quota, requested: requests.cpu=800m, used: requests.cpu=300m, limited: requests.cpu=1"],
            id = "only-our-quota",
        ),
    ],
)
def test_check_quota_exceeded(
    quotas:     List[client.V1ResourceQuota],
    requests:   Dict[str, str],
    pods:       int,
    violations: List[str],
):
    with pytest.raises(QuotaExceededError) as exc_info:
        _check_quota(_NAMESPACE, quotas, requests, pods)
    assert (exc_info.value.violations == violations)
    assert (exc_info.value.namespace  == _NAMESPACE)
    assert (str(exc_info.value) == f'Not enough quota in namespace "{_NAMESPACE}": {"; ".join(violations)}.')

# A quota created moments ago has no status yet: the check falls back to spec.hard and counts nothing as used.
@pytest.mark.parametrize(
    ("pods", "exceeded"),
    [(10, False), (11, True)],
    ids = ["fills-the-quota", "one-pod-too-many"],
)
def test_check_quota_without_status(pods: int, exceeded: bool):
    fresh: client.V1ResourceQuota = _quota("deployment-quota").model_copy(update={"status": None})
    requests: Dict[str, str] = {"cpu": "100m", "memory": "128Mi"}
    if not exceeded:
        _check_quota(_NAMESPACE, [fresh], requests, pods)
        return
    with pytest.raises(QuotaExceededError) as exc_info:
        _check_quota(_NAMESPACE, [fresh], requests, pods)
    assert (exc_info.value.violations == [
        "exceeded quota: deployment-quota, requested: requests.cpu=1100m, used: requests.cpu=0, limited: requests.cpu=1"
    ])

def test_check_quota_without_quotas():
    _check_quota(_NAMESPACE, [], {"cpu": "100", "memory": "100Gi"}, 100)

# ==================== Quantity formatting ====================

@pytest.mark.parametrize(
    ("value", "resource", "expected"),
    [
        (Decimal("0.300"),     "cpu",    "300m"),
        (Decimal("1"),         "cpu",    "1"),
        (Decimal("2.000"),     "cpu",    "2"),
        (Decimal("1.5"),       "cpu",    "1500m"),
        (Decimal("0.0005"),    "cpu",    "0.5m"),
        (Decimal("0"),         "cpu",    "0"),
        (Decimal(3 * 2 ** 25), "memory", "96Mi"),          # 3 x 32Mi
        (Decimal(2 ** 31),     "memory", "2Gi"),
        (Decimal(1536 * 1024), "memory", "1536Ki"),        # not a whole number of Mi
        (Decimal(1536),        "memory", "1536"),          # not a whole number of Ki
        (Decimal(10 ** 9),     "memory", "1000000000"),    # 1G is not a whole number of binary units
        (Decimal(0),           "memory", "0"),
    ],
)
def test_format_quantity(value: Decimal, resource: str, expected: str):
    assert (_format_quantity(value, resource) == expected)

# ==================== Error response ====================

def test_quota_exceeded_is_a_403():
    # Kubernetes answers a pod that would exceed a quota with 403 Forbidden; so does the API.
    violation: str = "exceeded quota: deployment-quota, requested: requests.cpu=800m, used: requests.cpu=300m, limited: requests.cpu=1"
    with app.test_request_context():
        response: Response = app.make_response(
            app.handle_user_exception(QuotaExceededError(namespace="demo", violations=[violation]))
        )
    assert (response.status_code == 403)
    assert (response.get_json()["error"] == {
        "code":    "QUOTA_EXCEEDED",
        "message": f'Not enough quota in namespace "demo": {violation}.',
    })
