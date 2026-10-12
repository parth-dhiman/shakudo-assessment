# Standard imports.
import time
from decimal import Decimal
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
)

# Third-party imports.
import yaml
from flask.testing import FlaskClient
from kubernetes import client as k8s
from kubernetes.utils import parse_quantity
from werkzeug.test import TestResponse

# Local imports.
from app.config import (
    QUOTA_CONFIG_PATH,
    QUOTA_NAME,
)
from app.k8s.api import (
    init_apps_api,
    init_core_api,
)

# Script-Level Constants.
_BASE_API_URL: str = "/api/deployments"
_BASE_SPEC: Dict[str, Any] = {
    "name":      "hello",
    "image":     "busybox",
    "resources": {"requests": {"cpu": "50m", "memory": "32Mi"}},
}
_RESTARTED_AT_ANNOTATION: str = "kubectl.kubernetes.io/restartedAt"
_REVISION_ANNOTATION:     str = "deployment.kubernetes.io/revision"
_CASCADE_TIMEOUT_SECONDS: int = 60
_REVISION_TIMEOUT_SECONDS: int = 30
_POD_TIMEOUT_SECONDS: int = 90

def _create_deployment(
    client:    FlaskClient,
    namespace: str,
    **fields:  Any,
) -> str:
    # fields: anything to add to the base spec, e.g. command.
    payload: Dict[str, Any] = {
        **_BASE_SPEC,
        "namespace": namespace,
        **fields,
    }
    response: TestResponse = client.post(
        _BASE_API_URL,
        json = payload,
    )
    assert (response.status_code == 201)
    return response.get_json()["uid"]

def _list_deployments(
    client:    FlaskClient,
    namespace: Optional[str] = None,
) -> List[str]:
    # Returns the listed UIDs; no namespace lists every namespace.
    response: TestResponse = client.get(
        _BASE_API_URL,
        query_string = {"namespace": namespace} if namespace else {},
    )
    assert (response.status_code == 200)
    return [item["uid"] for item in response.get_json()["items"]]

def _get_deployment(
    client: FlaskClient,
    uid:    str,
    status: int = 200,
) -> Dict[str, Any]:
    # The UID alone identifies the deployment; no namespace needed.
    response: TestResponse = client.get(
        f"{_BASE_API_URL}/{uid}",
    )
    assert (response.status_code == status)
    return response.get_json()

def _patch_deployment(
    client: FlaskClient,
    uid:    str,
    patch:  Dict[str, Any],
) -> Dict[str, Any]:
    response: TestResponse = client.patch(
        f"{_BASE_API_URL}/{uid}",
        json = patch,
    )
    assert (response.status_code == 200)
    body: Dict[str, Any] = response.get_json()
    assert (body["uid"] == uid)

    # The response has no pod template, so check the change on the cluster object itself.
    deployment: k8s.V1Deployment = init_apps_api().read_namespaced_deployment(
        name      = body["name"],
        namespace = body["namespace"],
    )
    if ("replicas" in patch):
        assert (deployment.spec.replicas == patch["replicas"])
    if ("image" in patch):
        assert (deployment.spec.template.spec.containers[0].image == patch["image"])
    return body

def _restart_deployment(
    client: FlaskClient,
    uid:    str,
) -> Dict[str, Any]:
    response: TestResponse = client.post(
        f"{_BASE_API_URL}/{uid}/restart",
    )
    assert (response.status_code == 201)
    body: Dict[str, Any] = response.get_json()
    assert (body["uid"] == uid)

    # Like `kubectl rollout restart`: the pod template carries the restartedAt annotation.
    deployment: k8s.V1Deployment = init_apps_api().read_namespaced_deployment(
        name      = body["name"],
        namespace = body["namespace"],
    )
    annotations: Dict[str, str] = deployment.spec.template.metadata.annotations or {}
    assert (_RESTARTED_AT_ANNOTATION in annotations)
    return body

def _delete_deployment(
    client:    FlaskClient,
    uid:       str,
    namespace: str,
) -> None:
    response: TestResponse = client.delete(
        f"{_BASE_API_URL}/{uid}",
    )
    assert (response.status_code == 204)

    # Background cascade: the garbage collector removes the owned ReplicaSets and Pods shortly after.
    # The test namespace holds only this deployment, so anything left in it is a leftover.
    apps_api: k8s.AppsV1Api = init_apps_api()
    core_api: k8s.CoreV1Api = init_core_api()
    deadline: float = time.monotonic() + _CASCADE_TIMEOUT_SECONDS
    while True:
        leftovers: List[str] = [
            obj.metadata.name
            for obj in (
                apps_api.list_namespaced_replica_set(namespace).items
                + core_api.list_namespaced_pod(namespace).items
            )
        ]
        if not leftovers:
            break
        assert (time.monotonic() < deadline), f"Not garbage-collected: {leftovers}"
        time.sleep(1)

def _wait_for_revision(
    namespace: str,
    revision:  int,
) -> None:
    # The controller numbers a pod template asynchronously; a rollback can only target revisions it has recorded.
    apps_api: k8s.AppsV1Api = init_apps_api()
    deadline: float = time.monotonic() + _REVISION_TIMEOUT_SECONDS
    while True:
        deployment: k8s.V1Deployment = apps_api.read_namespaced_deployment(
            name      = _BASE_SPEC["name"],
            namespace = namespace,
        )
        if ((deployment.metadata.annotations or {}).get(_REVISION_ANNOTATION) == str(revision)):
            return
        assert (time.monotonic() < deadline), f"Revision {revision} never recorded"
        time.sleep(0.5)

def _rollback_deployment(
    client: FlaskClient,
    uid:    str,
    body:   Optional[Dict[str, Any]] = None,
    status: int = 200,
) -> Dict[str, Any]:
    # No body rolls back to the previous revision.
    response: TestResponse = client.post(
        f"{_BASE_API_URL}/{uid}/rollback",
        json = body,
    )
    assert (response.status_code == status)
    return response.get_json()

def _rollback_info(
    client:    FlaskClient,
    uid:       str,
    namespace: Optional[str] = None,
    status:    int = 200,
) -> Dict[str, Any]:
    response: TestResponse = client.get(
        f"{_BASE_API_URL}/{uid}/history",
        query_string = {"namespace": namespace} if namespace else {},
    )
    assert (response.status_code == status)
    return response.get_json()

def _history(
    client: FlaskClient,
    uid:    str,
) -> List[Tuple[int, str, bool]]:
    # (revision, image, current) per history entry, newest first.
    return [(h["revision"], h["image"], h["current"]) for h in _rollback_info(client, uid)["history"]]

def _wait_for_pod(
    client: FlaskClient,
    uid:    str,
    check:  Callable[[Dict[str, Any]], bool],
) -> Dict[str, Any]:
    # Polls the detail view until one of the deployment's pods passes `check`, and returns that pod.
    deadline: float = time.monotonic() + _POD_TIMEOUT_SECONDS
    while True:
        for pod in _get_deployment(client, uid)["pods"]:
            if check(pod):
                return pod
        assert (time.monotonic() < deadline), "No pod reached the expected state"
        time.sleep(1)

def _is_running(pod: Dict[str, Any]) -> bool:
    return any("running" in (container["state"] or {}) for container in pod["containers"])

def _is_pulling_image(pod: Dict[str, Any]) -> bool:
    return any(
        ((container["state"] or {}).get("waiting") or {}).get("reason") in ("ErrImagePull", "ImagePullBackOff")
        for container in pod["containers"]
    )

def _log_events(
    client:   FlaskClient,
    uid:      str,
    pod:      str,
    previous: bool = False,
    until:    Callable[[List[Tuple[Optional[str], str]]], bool] = lambda events: False,
) -> List[Tuple[Optional[str], str]]:
    # Reads the Server-Sent Events as they arrive, as (event name or None for a log line, data),
    # until `until` holds or the server ends the stream.
    response: TestResponse = client.get(
        f"{_BASE_API_URL}/{uid}/logs",
        query_string = {"pod": pod, **({"previous": "true"} if previous else {})},
        buffered     = False,
    )
    assert (response.status_code == 200)
    assert (response.mimetype == "text/event-stream")
    events: List[Tuple[Optional[str], str]] = []
    buffer: str = ""
    try:
        for chunk in response.response:
            buffer += chunk.decode() if isinstance(chunk, bytes) else chunk
            *blocks, buffer = buffer.split("\n\n")
            for block in blocks:
                fields: List[List[str]] = [line.split(": ", 1) for line in block.split("\n")]
                data: List[str] = [value for (key, value) in fields if key == "data"]
                if data:  # a block without data (the retry hint) is not an event
                    events.append((next((value for (key, value) in fields if key == "event"), None), "\n".join(data)))
            if until(events):
                break
    finally:
        response.close()  # closes the server's stream, and with it the Kubernetes log connection
    return events

def _template(namespace: str) -> k8s.V1PodTemplateSpec:
    return init_apps_api().read_namespaced_deployment(
        name      = _BASE_SPEC["name"],
        namespace = namespace,
    ).spec.template

def test_lifecycle(
    client: FlaskClient,
    namespace: str,
):
    # Create.
    uid: str = _create_deployment(client, namespace)

    # It is listed, both under its namespace and across all namespaces.
    assert (_list_deployments(client, namespace) == [uid])
    assert (uid in _list_deployments(client))

    # Detail, read live from the cluster.
    deployment: Dict[str, Any] = _get_deployment(client, uid)
    assert (deployment["name"]      == _BASE_SPEC["name"])
    assert (deployment["namespace"] == namespace)
    assert (deployment["replicas"]["desired"] == 1)

    # Scale, and the replica count changes.
    _patch_deployment(client, uid, {"replicas": 3})
    assert (_get_deployment(client, uid)["replicas"]["desired"] == 3)

    # Image update (starts a rolling update).
    _patch_deployment(client, uid, {"image": "busybox:1.36"})

    # Rollout restart.
    _restart_deployment(client, uid)

    # Delete, and it is gone.
    _delete_deployment(client, uid, namespace)
    _get_deployment(client, uid, status=404)
    assert (_list_deployments(client, namespace) == [])

def test_namespace_quota(
    client: FlaskClient,
    namespace: str,
):
    # A namespace the API creates gets the ResourceQuota from quota.yaml.
    _create_deployment(client, namespace)
    with open(QUOTA_CONFIG_PATH) as f:
        requests: Dict[str, str] = yaml.safe_load(f)["requests"]
    core_api: k8s.CoreV1Api = init_core_api()
    quota: k8s.V1ResourceQuota = core_api.read_namespaced_resource_quota(
        name      = QUOTA_NAME,
        namespace = namespace,
    )
    assert (quota.spec.hard == {"requests.cpu": requests["cpu"], "requests.memory": requests["memory"]})

    # Kubernetes counts the deployment's pod against it (the quota controller updates status.used shortly after).
    expected: Dict[str, str] = {"requests.cpu": "50m", "requests.memory": "32Mi"}
    deadline: float = time.monotonic() + _POD_TIMEOUT_SECONDS
    while True:
        used: Dict[str, str] = (core_api.read_namespaced_resource_quota(
            name      = QUOTA_NAME,
            namespace = namespace,
        ).status.used or {})
        if (used == expected):
            break
        assert (time.monotonic() < deadline), f"Quota usage stayed at {used}"
        time.sleep(1)

def _wait_for_quota_cpu(
    namespace: str,
    cpu:       Decimal,
) -> None:
    # The quota controller counts new pods shortly after they appear; the API checks against that count.
    core_api: k8s.CoreV1Api = init_core_api()
    deadline: float = time.monotonic() + _POD_TIMEOUT_SECONDS
    while True:
        used: Dict[str, str] = core_api.read_namespaced_resource_quota(
            name      = QUOTA_NAME,
            namespace = namespace,
        ).status.used or {}
        if (parse_quantity(used.get("requests.cpu", "0")) == cpu):
            return
        assert (time.monotonic() < deadline), f"Quota usage stayed at {used}"
        time.sleep(1)

def test_quota_enforcement(
    client: FlaskClient,
    namespace: str,
):
    # Each pod requests a quarter of the namespace's CPU quota, so four pods fill it exactly.
    with open(QUOTA_CONFIG_PATH) as f:
        quarter: Decimal = parse_quantity(yaml.safe_load(f)["requests"]["cpu"]) / 4
    pod: Dict[str, Any] = {
        "command":   ["sleep", "3600"],
        "resources": {"requests": {"cpu": f"{int(quarter * 1000)}m", "memory": "32Mi"}},
    }

    # Two pods: half the quota, once Kubernetes has counted them.
    uid: str = _create_deployment(client, namespace, replicas=2, **pod)
    _wait_for_quota_cpu(namespace, 2 * quarter)

    # Three more pods do not fit: 403, and nothing is created.
    response: TestResponse = client.post(
        _BASE_API_URL,
        json = {**_BASE_SPEC, **pod, "namespace": namespace, "name": "hello-2", "replicas": 3},
    )
    assert (response.status_code == 403)
    error: Dict[str, Any] = response.get_json()["error"]
    assert (error["code"] == "QUOTA_EXCEEDED")
    assert ("exceeded quota: deployment-quota" in error["message"])
    assert (_list_deployments(client, namespace) == [uid])

    # Scaling past the quota is refused the same way, and the replica count stays as it was.
    response = client.patch(
        f"{_BASE_API_URL}/{uid}",
        json = {"replicas": 5},
    )
    assert (response.status_code == 403)
    assert (response.get_json()["error"]["code"] == "QUOTA_EXCEEDED")
    assert (_get_deployment(client, uid)["replicas"]["desired"] == 2)

    # Scaling to exactly the quota fits.
    _patch_deployment(client, uid, {"replicas": 4})
    _wait_for_quota_cpu(namespace, 4 * quarter)

    # With the quota full: zero replicas need no room, and scaling down is never refused.
    _create_deployment(client, namespace, name="hello-zero", replicas=0, **pod)
    _patch_deployment(client, uid, {"replicas": 1})

def test_rollback_info(
    client: FlaskClient,
    namespace: str,
):
    # Revision 1: the created template, read live from its ReplicaSet.
    uid: str = _create_deployment(client, namespace)
    _wait_for_revision(namespace, 1)
    info: Dict[str, Any] = _rollback_info(client, uid)
    assert (info["uid"]             == uid)
    assert (info["name"]            == _BASE_SPEC["name"])
    assert (info["namespace"]       == namespace)
    assert (info["currentRevision"] == 1)
    [entry] = info["history"]
    assert (entry["revision"] == 1)
    assert (entry["current"]  is True)
    assert (entry["image"]    == _BASE_SPEC["image"])
    assert (entry["replicaSet"].startswith(f"{_BASE_SPEC['name']}-"))
    assert isinstance(entry["replicas"], int)
    assert isinstance(entry["createdAt"], str)

    # An image update adds revision 2; the old one stays in the history.
    _patch_deployment(client, uid, {"image": "busybox:1.36"})
    _wait_for_revision(namespace, 2)
    assert (_rollback_info(client, uid)["currentRevision"] == 2)
    assert (_history(client, uid) == [
        (2, "busybox:1.36",      True),
        (1, _BASE_SPEC["image"], False),
    ])

    # Scaling does not change the pod template, so it adds no revision.
    _patch_deployment(client, uid, {"replicas": 2})
    assert ([revision for (revision, _, _) in _history(client, uid)] == [2, 1])

    # The namespace narrows the lookup; a namespace without the deployment is a 404.
    assert (_rollback_info(client, uid, namespace=namespace)["uid"] == uid)
    _rollback_info(client, uid, namespace=f"{namespace}-other", status=404)

    # Unknown and malformed UIDs.
    _rollback_info(client, "00000000-0000-0000-0000-000000000000", status=404)
    _rollback_info(client, "not-a-uid", status=400)

def test_rollback(
    client: FlaskClient,
    namespace: str,
):
    # Revision 1: the created template. Nothing to roll back to yet.
    uid: str = _create_deployment(client, namespace)
    _wait_for_revision(namespace, 1)
    error: Dict[str, Any] = _rollback_deployment(client, uid, status=400)["error"]
    assert (error["code"] == "REVISION_NOT_FOUND")

    # Revision 2: an image update.
    _patch_deployment(client, uid, {"image": "busybox:1.36"})
    _wait_for_revision(namespace, 2)

    # No body rolls back to the previous revision. The restored template becomes revision 3,
    # so revision 1 leaves the history (`kubectl rollout undo` renumbers the same way).
    body: Dict[str, Any] = _rollback_deployment(client, uid)
    assert (body["uid"]             == uid)
    assert (body["rolledBackTo"]    == 1)
    assert (body["skipped"]         is False)
    assert (body["currentRevision"] == 3)
    assert (_history(client, uid) == [
        (3, _BASE_SPEC["image"], True),
        (2, "busybox:1.36",      False),
    ])
    template: k8s.V1PodTemplateSpec = _template(namespace)
    assert (template.spec.containers[0].image == _BASE_SPEC["image"])
    assert ("pod-template-hash" not in (template.metadata.labels or {}))   # the controller's label is not copied back

    # Rolling back to the current revision changes nothing.
    body = _rollback_deployment(client, uid, {"revision": 3})
    assert (body["skipped"]         is True)
    assert (body["currentRevision"] == 3)

    # Revision 1 was renumbered to 3, so it is no longer in the history.
    error = _rollback_deployment(client, uid, {"revision": 1}, status=400)["error"]
    assert (error["code"] == "REVISION_NOT_FOUND")

    # An explicit revision: back to the image update, now revision 4.
    body = _rollback_deployment(client, uid, {"revision": 2})
    assert (body["rolledBackTo"]    == 2)
    assert (body["currentRevision"] == 4)
    assert ([revision for (revision, _, _) in _history(client, uid)] == [4, 3])
    assert (_template(namespace).spec.containers[0].image == "busybox:1.36")

    # Validation and unknown deployments.
    _rollback_deployment(client, uid, {"revision": 0}, status=400)
    _rollback_deployment(client, "00000000-0000-0000-0000-000000000000", status=404)

def test_logs(
    client: FlaskClient,
    namespace: str,
):
    # A container that prints three lines, stays up 5 s and exits, so it restarts and leaves a previous log.
    uid: str = _create_deployment(
        client,
        namespace,
        command = ["sh", "-c", "for i in 1 2 3; do echo line $i; done; sleep 5; exit 1"],
    )
    pod: str = _wait_for_pod(client, uid, _is_running)["name"]

    # Live: the last lines arrive at once; the stream ends when the container exits (the browser then reconnects).
    events: List[Tuple[Optional[str], str]] = _log_events(
        client, uid, pod,
        until = lambda events: bool(events) and (events[-1][0] == "end"),
    )
    assert ([data for (name, data) in events if name is None] == ["line 1", "line 2", "line 3"])
    assert (events[-1] == ("end", ""))

    # Previous: once the container has restarted, the earlier container's log is complete and simply ends.
    _wait_for_pod(client, uid, lambda pod: pod["restartCount"] >= 1)
    events = _log_events(client, uid, pod, previous=True)
    assert ([data for (name, data) in events if name is None] == ["line 1", "line 2", "line 3"])
    assert (events[-1] == ("end", ""))

    # A pod outside the deployment, or none at all, is rejected before any streaming starts.
    assert (client.get(f"{_BASE_API_URL}/{uid}/logs", query_string={"pod": "not-this-pod"}).status_code == 404)
    assert (client.get(f"{_BASE_API_URL}/{uid}/logs").status_code == 400)

    # A container that cannot start has no log yet: Kubernetes' reason arrives as an "unavailable" event, not a 500.
    _patch_deployment(client, uid, {"image": "busybox:nonexistent"})
    stuck: str = _wait_for_pod(client, uid, _is_pulling_image)["name"]
    events = _log_events(client, uid, stuck)
    assert ([name for (name, _) in events] == ["unavailable", "end"])
    assert ("is waiting to start" in events[0][1])
