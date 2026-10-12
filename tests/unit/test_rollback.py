# Standard imports.
import json
from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
)

# Third-party imports.
import pytest
from flask import Response
from flask.testing import FlaskClient
from kubernetes import client

# Local imports.
from app.api import app
from app.exceptions import RevisionNotFoundError
from app.k8s.services.deployments import (
    _is_current_template,
    _rollback_target,
    _rollback_template,
    _rollout_history,
)

# ==================== Snapshot ====================

# Recorded with `kubectl get deployments,replicasets -n <ns> -o json` after: create (busybox), image -> busybox:1.36, rollback.
# Revision 1 was restored as revision 3, so the history holds revisions 3 (busybox, current) and 2 (busybox:1.36).
_SNAPSHOT: Path = Path(__file__).parent.parent / "fixtures" / "rolled_back.json"

def _load_snapshot() -> Tuple[client.V1Deployment, List[client.V1ReplicaSet]]:
    items: List[Dict[str, Any]] = json.loads(_SNAPSHOT.read_text())["items"]
    [deployment] = [client.V1Deployment.from_dict(item) for item in items if item["kind"] == "Deployment"]
    replica_sets: List[client.V1ReplicaSet] = [
        client.V1ReplicaSet.from_dict(item) for item in items if item["kind"] == "ReplicaSet"
    ]
    return (deployment, replica_sets)

_DEPLOYMENT, _REPLICA_SETS = _load_snapshot()

def _replica_set(revision: int) -> client.V1ReplicaSet:
    return next(
        rs for rs in _REPLICA_SETS
        if rs.metadata.annotations["deployment.kubernetes.io/revision"] == str(revision)
    )

# ==================== History (rollback info) ====================

def test_history_lists_revisions_newest_first():
    history: List[Dict[str, Any]] = _rollout_history(_DEPLOYMENT, _REPLICA_SETS)
    assert ([(h["revision"], h["image"], h["current"]) for h in history] == [
        (3, "busybox",      True),
        (2, "busybox:1.36", False),
    ])

def test_history_entries_describe_their_replica_sets():
    history: List[Dict[str, Any]] = _rollout_history(_DEPLOYMENT, _REPLICA_SETS)
    for (entry, rs) in zip(history, [_replica_set(3), _replica_set(2)]):
        assert (entry["replicaSet"] == rs.metadata.name)
        assert (entry["replicas"]   == (rs.status.replicas or 0))
        assert (datetime.fromisoformat(entry["createdAt"]) == rs.metadata.creation_timestamp)

def test_history_order_does_not_depend_on_list_order():
    assert (_rollout_history(_DEPLOYMENT, _REPLICA_SETS[::-1]) == _rollout_history(_DEPLOYMENT, _REPLICA_SETS))

def test_history_without_replica_sets():
    assert (_rollout_history(_DEPLOYMENT, []) == [])

# ==================== Target revision (rollback) ====================

@pytest.mark.parametrize(
    ("revision", "expected"),
    [(None, 2), (2, 2), (3, 3)],
    ids = ["omitted-means-previous", "explicit-older", "explicit-current"],
)
def test_rollback_target(revision: Optional[int], expected: int):
    target: client.V1ReplicaSet = _rollback_target(_DEPLOYMENT, _REPLICA_SETS, revision)
    assert (target.metadata.name == _replica_set(expected).metadata.name)

def test_rollback_target_does_not_depend_on_list_order():
    target: client.V1ReplicaSet = _rollback_target(_DEPLOYMENT, _REPLICA_SETS[::-1], None)
    assert (target.metadata.name == _replica_set(2).metadata.name)

@pytest.mark.parametrize("revision", [
    1,                       # restored as revision 3, so it left the history
    4,
    99,
])
def test_rollback_target_missing_revision(revision: int):
    with pytest.raises(RevisionNotFoundError) as exc_info:
        _rollback_target(_DEPLOYMENT, _REPLICA_SETS, revision)
    assert (exc_info.value.revision == revision)
    assert (exc_info.value.name     == _DEPLOYMENT.metadata.name)

@pytest.mark.parametrize(
    "replica_sets",
    [[_replica_set(3)], []],
    ids = ["only-the-current-revision", "no-replica-sets"],
)
def test_rollback_target_without_previous_revision(replica_sets: List[client.V1ReplicaSet]):
    with pytest.raises(RevisionNotFoundError) as exc_info:
        _rollback_target(_DEPLOYMENT, replica_sets, None)
    assert (exc_info.value.revision is None)
    assert ("no previous revision" in str(exc_info.value))

# ==================== Template (rollback) ====================

@pytest.mark.parametrize("revision", [2, 3])
def test_rollback_template_drops_pod_template_hash(revision: int):
    rs: client.V1ReplicaSet = _replica_set(revision)
    labels: Dict[str, str] = _rollback_template(rs)["metadata"]["labels"]
    # Only the controller's label goes; the selector's labels stay, and the ReplicaSet itself is left untouched.
    assert ("pod-template-hash" not in labels)
    assert (labels == {k: v for k, v in rs.spec.template.metadata.labels.items() if k != "pod-template-hash"})
    assert ("pod-template-hash" in rs.spec.template.metadata.labels)

def test_rollback_template_is_kubernetes_json():
    # The JSON patch sends this dict as-is, so it must use the API's camelCase keys.
    container: Dict[str, Any] = _rollback_template(_replica_set(2))["spec"]["containers"][0]
    assert (container["image"] == "busybox:1.36")
    assert ("terminationMessagePath" in container)

@pytest.mark.parametrize(
    ("revision", "expected"),
    [(3, True), (2, False)],
    ids = ["current-revision-is-skipped", "older-revision-is-rolled-back"],
)
def test_is_current_template(revision: int, expected: bool):
    assert (_is_current_template(_DEPLOYMENT, _rollback_template(_replica_set(revision))) is expected)

# ==================== Routes ====================

_UID: str = "e5f1f2f8-2939-43ed-8e53-c8c980b220e2"

# Each request fails validation before any Kubernetes call, so no cluster is needed.
@pytest.mark.parametrize(
    ("method", "path", "kwargs", "field"),
    [
        ("get",  "/api/deployments/not-a-uid/history",  {},                                                  "uid"),
        ("get",  f"/api/deployments/{_UID}/history",    {"query_string": {"namespace": "Bad_NS"}},            "namespace"),
        ("post", "/api/deployments/not-a-uid/rollback", {},                                                  "uid"),
        ("post", f"/api/deployments/{_UID}/rollback",   {"query_string": {"namespace": "Bad_NS"}},            "namespace"),
        ("post", f"/api/deployments/{_UID}/rollback",   {"json": {"revision": 0}},                           "revision"),
        ("post", f"/api/deployments/{_UID}/rollback",   {"json": {"to_revision": 2}},                        "to_revision"),
        ("post", f"/api/deployments/{_UID}/rollback",   {"json": [2]},                                       "body"),
        ("post", f"/api/deployments/{_UID}/rollback",   {"data": "{bad", "content_type": "application/json"}, "body"),
    ],
    ids = [
        "history-bad-uid",
        "history-bad-namespace",
        "rollback-bad-uid",
        "rollback-bad-namespace",
        "rollback-revision-zero",
        "rollback-unknown-field",
        "rollback-body-not-an-object",
        "rollback-malformed-json",
    ],
)
def test_invalid_requests(
    client: FlaskClient,
    method: str,
    path:   str,
    kwargs: Dict[str, Any],
    field:  str,
):
    response = getattr(client, method)(path, **kwargs)
    assert (response.status_code == 400)
    error: Dict[str, Any] = response.get_json()["error"]
    assert (error["code"] == "INVALID_PAYLOAD")
    assert ([detail["field"] for detail in error["details"]] == [field])

def test_revision_not_found_is_a_400():
    # The deployment exists, so an unknown revision is a bad request, not a 404.
    with app.test_request_context():
        response: Response = app.make_response(
            app.handle_user_exception(RevisionNotFoundError(name="web", revision=9))
        )
    assert (response.status_code == 400)
    assert (response.get_json()["error"] == {
        "code":    "REVISION_NOT_FOUND",
        "message": 'Revision 9 not found in the rollout history of deployment "web".',
    })
