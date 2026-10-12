# Standard imports.
from typing import (
    Any,
    Dict,
    Generator,
    Iterable,
    Iterator,
    List,
    Optional,
    Tuple,
)
from datetime import datetime
from decimal import Decimal
import json
import time

# Third-party imports.
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.utils import parse_quantity
from urllib3.exceptions import (
    HTTPError,
    ProtocolError,
    ReadTimeoutError,
)

# Local imports.
from app.k8s.api import (
    init_core_api,
    init_apps_api,
)
from app.config import (
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
)
from app.schema.deployments import (
    DeploymentSpec,
    Namespace,
    NamespaceAndUid,
    PatchSpec,
    RollbackSpec,
)
from app.k8s.services.common import (
    namespace_exists,
    create_namespace,
)
from app.k8s.services.services import create_service
from app.exceptions import (
    NotFoundError,
    PodNotFoundError,
    QuotaExceededError,
    RevisionNotFoundError,
)

# The deployment controller numbers each pod template it rolls out, on the Deployment and on that template's ReplicaSet.
# https://github.com/kubernetes/kubernetes/blob/master/pkg/controller/deployment/util/deployment_util.go
_REVISION_ANNOTATION = "deployment.kubernetes.io/revision"

# How long a rollback waits for the controller to renumber revisions before returning.
_ROLLBACK_SYNC_TIMEOUT_SECONDS = 5

# Converts client objects to Kubernetes JSON (camelCase). Needs no cluster connection, so the rollback helpers stay pure.
_sanitize = client.ApiClient().sanitize_for_serialization

# A log stream starts with the last lines of the container's log, then follows it.
_LOG_TAIL_LINES = 100

# A followed log that stays silent this long is ended; the browser reconnects, which also notices a closed tab.
_LOG_CONNECT_TIMEOUT_SECONDS = 5
_LOG_IDLE_SECONDS            = 15

# ResourceQuota keys that cap requests, and the container request each one counts ("cpu" means requests.cpu).
# https://kubernetes.io/docs/concepts/policy/resource-quotas/
_REQUEST_QUOTA_KEYS: Dict[str, str] = {
    "requests.cpu":    "cpu",
    "cpu":             "cpu",
    "requests.memory": "memory",
    "memory":          "memory",
}

def _read_managed_deployment(
    namespace: Optional[str],
    uid:       str,
) -> client.V1Deployment:

    apps_api: client.AppsV1Api = init_apps_api()
    label_selector: str = f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}"

    # metadata.uid is not a supported field selector, so List the managed deployments and match by UID.
    # Without a namespace, search all of them: UIDs are unique cluster-wide.
    # https://kubernetes.io/docs/concepts/overview/working-with-objects/field-selectors/
    if (namespace is not None):
        deployments: client.V1DeploymentList = apps_api.list_namespaced_deployment(
            namespace      = namespace,
            label_selector = label_selector,
        )
    else:
        deployments: client.V1DeploymentList = apps_api.list_deployment_for_all_namespaces(
            label_selector = label_selector,
        )
    for deployment in deployments.items:
        if (deployment.metadata.uid == uid):
            return deployment

    raise NotFoundError(
        namespace = namespace,
        uid       = uid,
    )

def _build_deployment(deployment_spec: DeploymentSpec) -> Dict[str, Any]:

    # System labels go on the Deployment and the pod template; the schema rejects user labels with these keys.
    system_labels: Dict[str, str] = {
        "app.kubernetes.io/name": deployment_spec.name,
        MANAGED_BY_LABEL:         MANAGED_BY_VALUE,
    }
    labels: Dict[str, str] = {**(deployment_spec.labels or {}), **system_labels}

    # Container: set only the optional fields that were provided.
    container: Dict[str, Any] = {
        "name":  deployment_spec.name,
        "image": deployment_spec.image,
    }
    if (deployment_spec.port is not None):
        container["ports"] = [{"containerPort": deployment_spec.port}]
    if (deployment_spec.command is not None):
        container["command"] = deployment_spec.command
    if (deployment_spec.args is not None):
        container["args"] = deployment_spec.args
    if (deployment_spec.env is not None):
        container["env"] = [{"name": k, "value": v} for k, v in deployment_spec.env.items()]
    if (deployment_spec.resources is not None):
        container["resources"] = deployment_spec.resources.model_dump(exclude_none=True)
    if (deployment_spec.liveness_probe is not None):
        container["livenessProbe"] = deployment_spec.liveness_probe.model_dump(exclude_none=True, by_alias=True)
    if (deployment_spec.readiness_probe is not None):
        container["readinessProbe"] = deployment_spec.readiness_probe.model_dump(exclude_none=True, by_alias=True)

    # apps/v1 does not default the selector; it must match the template labels and is immutable.
    # Selecting on both system labels avoids overlapping unmanaged workloads.
    # https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#selector
    return {
        "apiVersion": "apps/v1",
        "kind":       "Deployment",
        "metadata": {
            "name":      deployment_spec.name,
            "namespace": deployment_spec.namespace,
            "labels":    labels,
        },
        "spec": {
            "replicas": deployment_spec.replicas,
            "selector": {"matchLabels": system_labels},
            "template": {
                "metadata": {"labels": labels},
                "spec":     {"containers": [container]},
            },
        },
    }

def _format_quantity(value: Decimal, resource: str) -> str:
    # A computed total, written the way Kubernetes writes quantities: "300m" or "2" for CPU, "96Mi" for memory.
    if (resource == "cpu"):
        if (value == value.to_integral_value()):
            return f"{value.normalize():f}"
        return f"{(value * 1000).normalize():f}m"
    for (suffix, factor) in (("Gi", 2 ** 30), ("Mi", 2 ** 20), ("Ki", 2 ** 10)):
        if (value >= factor) and (value % factor == 0):
            return f"{(value / factor).normalize():f}{suffix}"
    return f"{value.normalize():f}"

def _check_quota(
    namespace: str,
    quotas:    List[client.V1ResourceQuota],
    requests:  Dict[str, str],
    pods:      int,
) -> None:
    # Raises QuotaExceededError when `pods` more pods, each with `requests`, would take any quota past its limit.
    # status.used is the quota controller's sum over every live pod in the namespace; a missing entry means none yet.
    violations: List[str] = []
    for quota in quotas:
        status: client.V1ResourceQuotaStatus = quota.status or client.V1ResourceQuotaStatus()
        hard: Dict[str, str] = status.hard or (quota.spec.hard if quota.spec else None) or {}
        used: Dict[str, str] = status.used or {}
        exceeded: List[Tuple[str, str, str, str]] = []
        for (key, limit) in sorted(hard.items()):
            resource: Optional[str] = _REQUEST_QUOTA_KEYS.get(key)
            if (resource is None) or (requests.get(resource) is None):
                continue
            requested: Decimal = parse_quantity(requests[resource]) * pods
            in_use: str = used.get(key, "0")
            if (parse_quantity(in_use) + requested > parse_quantity(limit)):
                exceeded.append((key, _format_quantity(requested, resource), in_use, limit))
        if exceeded:
            # Worded like Kubernetes' own refusal of a pod.
            violations.append(
                f"exceeded quota: {quota.metadata.name}, "
                f"requested: {','.join(f'{key}={value}' for (key, value, _, _) in exceeded)}, "
                f"used: {','.join(f'{key}={value}' for (key, _, value, _) in exceeded)}, "
                f"limited: {','.join(f'{key}={value}' for (key, _, _, value) in exceeded)}"
            )
    if violations:
        raise QuotaExceededError(
            namespace  = namespace,
            violations = violations,
        )

def _enforce_quota(
    namespace: str,
    requests:  Dict[str, str],
    pods:      int,
) -> None:
    # Kubernetes enforces quotas per pod: it would accept the Deployment and then refuse its pods. So check first.
    # Not atomic: two requests at once can both pass; Kubernetes still refuses whichever pods do not fit.
    if (pods <= 0):
        return
    core_api: client.CoreV1Api = init_core_api()
    quotas: List[client.V1ResourceQuota] = core_api.list_namespaced_resource_quota(namespace=namespace).items
    _check_quota(namespace, quotas, requests, pods)

def create_deployment(deployment_spec: DeploymentSpec) -> Dict[str, Any]:

    # Access the namespace, ensure it exists.
    namespace: str = deployment_spec.namespace
    if not namespace_exists(namespace):
        create_namespace(namespace)

    # Raises QuotaExceededError unless every replica's requests fit the namespace's quotas.
    _enforce_quota(namespace, deployment_spec.resources.requests.model_dump(), deployment_spec.replicas)

    # Build the Deployments object as a Dict object.
    body: Dict[str, Any] = _build_deployment(deployment_spec)

    # Create the object.
    apps_api: client.AppsV1Api = init_apps_api()
    deployment = apps_api.create_namespaced_deployment(
        body      = body,
        namespace = deployment_spec.namespace,
    )

    try:
        service_info: Dict[str, Any] = create_service(
            deployment = deployment,
            service_spec = deployment_spec.service,
        ) if (deployment_spec.service is not None) else {}
    except Exception:
        # Roll back so a failed POST leaves nothing behind.
        delete_deployment(NamespaceAndUid(
            namespace = namespace,
            uid       = deployment.metadata.uid,
        ))
        raise

    # Return details in dict.
    return {
        "uid": deployment.metadata.uid,
        # Just created: no pods exist yet and the controller has not observed it, so this is UNKNOWN.
        "status": _compute_status(deployment, []),
        **service_info,
    }

def _deployment_info(deployment: client.V1Deployment) -> Dict[str, Any]:
    # Get detailed status for a single deployment including: replica counts (desired, ready, available, unavailable), pod-level status for each pod (name, phase, restart count, container statuses), conditions array from the Deployment status.

    apps_api: client.AppsV1Api = init_apps_api()
    core_api: client.CoreV1Api = init_core_api()
    sanitize = apps_api.api_client.sanitize_for_serialization

    namespace: str = deployment.metadata.namespace
    uid:       str = deployment.metadata.uid
    status: client.V1DeploymentStatus = deployment.status or client.V1DeploymentStatus()

    # Owned pods: Deployment -> ReplicaSet -> Pod through controller ownerReferences, matched by UID.
    # The selector only narrows the List calls (pods carry the template labels plus pod-template-hash).
    # https://kubernetes.io/docs/concepts/overview/working-with-objects/owners-dependents/
    selector: str = ",".join(
        f"{key}={value}" for key, value in (deployment.spec.selector.match_labels or {}).items()
    )
    replica_sets = apps_api.list_namespaced_replica_set(
        namespace      = namespace,
        label_selector = selector,
    ).items
    replica_set_uids = {
        rs.metadata.uid for rs in replica_sets
        if any(ref.controller and ref.uid == uid for ref in (rs.metadata.owner_references or []))
    }
    pods = [
        pod for pod in core_api.list_namespaced_pod(namespace=namespace, label_selector=selector).items
        if any(ref.controller and ref.uid in replica_set_uids for ref in (pod.metadata.owner_references or []))
    ]

    # Pod-level status; containerStatuses is absent until the pod is scheduled.
    pod_infos: List[Dict[str, Any]] = []
    for pod in pods:
        container_statuses = pod.status.container_statuses or []
        pod_infos.append({
            "name":         pod.metadata.name,
            "phase":        pod.status.phase,
            "restartCount": sum(cs.restart_count for cs in container_statuses),
            "node":         pod.spec.node_name,
            "containers": [
                {
                    "name":         cs.name,
                    "image":        cs.image,
                    "ready":        cs.ready,
                    "restartCount": cs.restart_count,
                    "state":        sanitize(cs.state),
                }
                for cs in container_statuses
            ],
        })

    # Deployments have no "last updated" field: use the newest condition lastUpdateTime, else the creation time.
    last_updated: datetime = max(
        (c.last_update_time for c in (status.conditions or []) if c.last_update_time),
        default = deployment.metadata.creation_timestamp,
    )

    # Replica counts in status are omitted when zero.
    return {
        "uid":               uid,
        "name":              deployment.metadata.name,
        "namespace":         namespace,
        "creationTimestamp": sanitize(deployment.metadata.creation_timestamp),
        "lastUpdated":       sanitize(last_updated),
        "replicas": {
            "desired":     deployment.spec.replicas,
            "ready":       status.ready_replicas or 0,
            "available":   status.available_replicas or 0,
            "unavailable": status.unavailable_replicas or 0,
        },
        "conditions": sanitize(status.conditions or []),
        "pods":       pod_infos,
        "status":     _compute_status(deployment, pods),
    }

def list_deployments(spec: Namespace) -> Dict[str, Any]:
    apps_api: client.AppsV1Api = init_apps_api()
    label_selector: str = f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}"

    namespace: Optional[str] = spec.namespace
    if (namespace is not None):
        # Look for deployments within namespace, should have our managed label.
        deployments: client.V1DeploymentList = apps_api.list_namespaced_deployment(
            namespace      = namespace,
            label_selector = label_selector,
        )
    else:
        # Look for deployments in ALL namespaces, should have our managed label.
        deployments: client.V1DeploymentList = apps_api.list_deployment_for_all_namespaces(
            label_selector = label_selector,
        )

    # Iterate over each deployment, gets its info from _deployment_info.
    return {
        "items": [_deployment_info(deployment) for deployment in deployments.items],
    }

def patch_deployment(spec: NamespaceAndUid, patch: PatchSpec) -> Dict[str, Any]:
    """
    Update a deployment. Must support at minimum: scaling replica count, updating the container image (triggering a rolling update).
    """

    # Raises NotFoundError unless the deployment exists and carries our managed label.
    deployment: client.V1Deployment = _read_managed_deployment(spec.namespace, spec.uid)

    # Scaling up: raises QuotaExceededError unless the extra pods fit (status.used already counts the running ones).
    current: int = deployment.spec.replicas or 0
    if (patch.replicas is not None) and (patch.replicas > current):
        container: client.V1Container = deployment.spec.template.spec.containers[0]
        requests: Dict[str, str] = (container.resources.requests if container.resources else None) or {}
        _enforce_quota(deployment.metadata.namespace, requests, patch.replicas - current)

    # Scaling only changes spec.replicas; an image change edits the pod template, which starts a rolling update.
    # Strategic merge patch merges containers by name, so only the image of our single container changes.
    # https://kubernetes.io/docs/tasks/manage-kubernetes-objects/update-api-object-kubectl-patch/
    body: Dict[str, Any] = {"spec": {}}
    if (patch.replicas is not None):
        body["spec"]["replicas"] = patch.replicas
    if (patch.image is not None):
        container_name: str = deployment.spec.template.spec.containers[0].name
        body["spec"]["template"] = {"spec": {"containers": [{"name": container_name, "image": patch.image}]}}

    apps_api: client.AppsV1Api = init_apps_api()
    patched: client.V1Deployment = apps_api.patch_namespaced_deployment(
        name          = deployment.metadata.name,
        namespace     = deployment.metadata.namespace,
        body          = body,
        _content_type = "application/strategic-merge-patch+json",
    )
    return _deployment_info(patched)

def get_deployment(spec: NamespaceAndUid) -> Dict[str, Any]:
    namespace: str = spec.namespace
    uid: str       = spec.uid
    deployment: client.V1Deployment = _read_managed_deployment(namespace, uid)
    return _deployment_info(deployment)

def delete_deployment(spec: NamespaceAndUid) -> None:
    """
    Delete the deployment and its associated resources from the cluster. Should handle cascade deletion of owned ReplicaSets and Pods."""

    # Raises NotFoundError unless the deployment exists and carries our managed label.
    deployment: client.V1Deployment = _read_managed_deployment(spec.namespace, spec.uid)

    # Background cascade: the Deployment goes at once, the garbage collector then removes its ReplicaSets and Pods.
    # The UID precondition guarantees we delete the object we resolved, not a same-name replacement.
    # https://kubernetes.io/docs/concepts/architecture/garbage-collection/#cascading-deletion
    apps_api: client.AppsV1Api = init_apps_api()
    apps_api.delete_namespaced_deployment(
        name      = deployment.metadata.name,
        namespace = deployment.metadata.namespace,
        body      = client.V1DeleteOptions(
            propagation_policy = "Background",
            preconditions      = client.V1Preconditions(uid=spec.uid),
        ),
    )

def restart_deployment(spec: NamespaceAndUid) -> Dict[str, Any]:
    """
    Trigger a rollout restart (equivalent to kubectl rollout restart). This should patch the pod template annotation to force new pods.
    """

    # Raises NotFoundError unless the deployment exists and carries our managed label.
    deployment: client.V1Deployment = _read_managed_deployment(spec.namespace, spec.uid)

    # Same change as `kubectl rollout restart`: set the restartedAt pod template annotation to the current RFC 3339 time.
    # A changed pod template makes the controller roll out a new ReplicaSet.
    # https://github.com/kubernetes/kubectl/blob/master/pkg/polymorphichelpers/objectrestarter.go
    body: Dict[str, Any] = {
        "spec": {"template": {"metadata": {"annotations": {
            "kubectl.kubernetes.io/restartedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        }}}},
    }
    apps_api: client.AppsV1Api = init_apps_api()
    patched: client.V1Deployment = apps_api.patch_namespaced_deployment(
        name          = deployment.metadata.name,
        namespace     = deployment.metadata.namespace,
        body          = body,
        _content_type = "application/strategic-merge-patch+json",
    )
    return _deployment_info(patched)

def _revision(obj: client.V1Deployment | client.V1ReplicaSet) -> int:
    # The controller writes the revision as a decimal string; an object it has not handled yet has none.
    return int((obj.metadata.annotations or {}).get(_REVISION_ANNOTATION, 0))

def _owned_replica_sets(deployment: client.V1Deployment) -> List[client.V1ReplicaSet]:
    # Same ownership rule as _deployment_info: a controller ownerReference to the Deployment's UID.
    apps_api: client.AppsV1Api = init_apps_api()
    selector: str = ",".join(
        f"{key}={value}" for key, value in (deployment.spec.selector.match_labels or {}).items()
    )
    return [
        rs for rs in apps_api.list_namespaced_replica_set(
            namespace      = deployment.metadata.namespace,
            label_selector = selector,
        ).items
        if any(ref.controller and ref.uid == deployment.metadata.uid for ref in (rs.metadata.owner_references or []))
    ]

def _rollout_history(
    deployment:   client.V1Deployment,
    replica_sets: List[client.V1ReplicaSet],
) -> List[Dict[str, Any]]:
    # One entry per owned ReplicaSet, newest revision first (what `kubectl rollout history` lists).
    current: int = _revision(deployment)
    return [
        {
            "revision":   _revision(rs),
            "current":    _revision(rs) == current,
            "replicaSet": rs.metadata.name,
            "image":      rs.spec.template.spec.containers[0].image,
            "replicas":   (rs.status.replicas if rs.status else None) or 0,
            "createdAt":  _sanitize(rs.metadata.creation_timestamp),
        }
        for rs in sorted(replica_sets, key=_revision, reverse=True)
    ]

def _rollback_target(
    deployment:   client.V1Deployment,
    replica_sets: List[client.V1ReplicaSet],
    revision:     Optional[int],
) -> client.V1ReplicaSet:
    # Every revision still in history is an owned ReplicaSet holding that revision's pod template.
    # No revision means the previous one: the second-highest, as kubectl's --to-revision=0.
    # https://github.com/kubernetes/kubectl/blob/master/pkg/polymorphichelpers/rollback.go
    newest_first: List[client.V1ReplicaSet] = sorted(replica_sets, key=_revision, reverse=True)
    if (revision is None):
        target: Optional[client.V1ReplicaSet] = newest_first[1] if (len(newest_first) > 1) else None
    else:
        target: Optional[client.V1ReplicaSet] = next(
            (rs for rs in newest_first if _revision(rs) == revision),
            None,
        )
    if (target is None):
        raise RevisionNotFoundError(
            name     = deployment.metadata.name,
            revision = revision,
        )
    return target

def _rollback_template(replica_set: client.V1ReplicaSet) -> Dict[str, Any]:
    # The ReplicaSet's template is the Deployment's template at that revision plus the controller's pod-template-hash label.
    template: Dict[str, Any] = _sanitize(replica_set.spec.template)
    template.get("metadata", {}).get("labels", {}).pop("pod-template-hash", None)
    return template

def _is_current_template(
    deployment: client.V1Deployment,
    template:   Dict[str, Any],
) -> bool:
    # Already running that template: nothing to roll back (kubectl reports "skipped rollback").
    return (template == _sanitize(deployment.spec.template))

def _wait_for_controller(deployment: client.V1Deployment) -> client.V1Deployment:
    # Re-read the Deployment until the controller has observed its latest spec (or the timeout passes).
    # The controller renumbers revisions during that sync, so history read before it is stale.
    apps_api: client.AppsV1Api = init_apps_api()
    deadline: float = time.monotonic() + _ROLLBACK_SYNC_TIMEOUT_SECONDS
    while True:
        current: client.V1Deployment = apps_api.read_namespaced_deployment(
            name      = deployment.metadata.name,
            namespace = deployment.metadata.namespace,
        )
        observed: int = (current.status.observed_generation if current.status else None) or 0
        if (observed >= current.metadata.generation) or (time.monotonic() >= deadline):
            return current
        time.sleep(0.2)

def rollback_deployment(spec: NamespaceAndUid, rollback: RollbackSpec) -> Dict[str, Any]:
    """
    Roll back to an earlier revision (equivalent to kubectl rollout undo).
    """

    # Raises NotFoundError unless the deployment exists and carries our managed label.
    deployment: client.V1Deployment = _read_managed_deployment(spec.namespace, spec.uid)

    # Raises RevisionNotFoundError unless that revision (or a previous one) is still in the history.
    target: client.V1ReplicaSet = _rollback_target(deployment, _owned_replica_sets(deployment), rollback.revision)
    template: Dict[str, Any] = _rollback_template(target)

    skipped: bool = _is_current_template(deployment, template)
    if not skipped:
        # Replace the whole template with a JSON patch, as kubectl does:
        # a strategic merge would keep anything added since that revision (env vars, probes, ...).
        apps_api: client.AppsV1Api = init_apps_api()
        patched: client.V1Deployment = apps_api.patch_namespaced_deployment(
            name          = deployment.metadata.name,
            namespace     = deployment.metadata.namespace,
            body          = [{"op": "replace", "path": "/spec/template", "value": template}],
            _content_type = "application/json-patch+json",
        )
        # The restored template becomes a new revision (its ReplicaSet is renumbered to the highest).
        # Waiting for that keeps currentRevision, and any history read right after this call, up to date.
        deployment = _wait_for_controller(patched)

    # rolledBackTo uses the numbering from before the rollback (target was read before the patch).
    return {
        "uid":             deployment.metadata.uid,
        "name":            deployment.metadata.name,
        "namespace":       deployment.metadata.namespace,
        "rolledBackTo":    _revision(target),
        "skipped":         skipped,
        "currentRevision": _revision(deployment),
    }

def rollback_info(spec: NamespaceAndUid) -> Dict[str, Any]:
    """
    Rollout history (equivalent to kubectl rollout history): the revisions a rollback can target.
    """

    # Raises NotFoundError unless the deployment exists and carries our managed label.
    deployment: client.V1Deployment = _read_managed_deployment(spec.namespace, spec.uid)
    return {
        "uid":             deployment.metadata.uid,
        "name":            deployment.metadata.name,
        "namespace":       deployment.metadata.namespace,
        "currentRevision": _revision(deployment),
        "history":         _rollout_history(deployment, _owned_replica_sets(deployment)),
    }

def _owned_pods(deployment: client.V1Deployment) -> List[client.V1Pod]:
    # Same ownership rule as _deployment_info: Deployment -> ReplicaSet -> Pod through controller ownerReferences.
    core_api: client.CoreV1Api = init_core_api()
    replica_set_uids = {rs.metadata.uid for rs in _owned_replica_sets(deployment)}
    selector: str = ",".join(
        f"{key}={value}" for key, value in (deployment.spec.selector.match_labels or {}).items()
    )
    return [
        pod for pod in core_api.list_namespaced_pod(
            namespace      = deployment.metadata.namespace,
            label_selector = selector,
        ).items
        if any(ref.controller and ref.uid in replica_set_uids for ref in (pod.metadata.owner_references or []))
    ]

def _log_lines(chunks: Iterable[bytes]) -> Iterator[str]:
    # Chunks can end mid-line, even mid-character: split on newlines first and decode whole lines only.
    buffer: bytes = b""
    for chunk in chunks:
        buffer += chunk
        *lines, buffer = buffer.split(b"\n")
        for line in lines:
            yield line.decode("utf-8", errors="replace").rstrip("\r")
    if buffer:
        yield buffer.decode("utf-8", errors="replace").rstrip("\r")

def _api_message(e: ApiException) -> str:
    # Kubernetes puts the reason in the Status body's message.
    try:
        return json.loads(e.body)["message"]
    except (TypeError, ValueError, KeyError):
        return e.reason or "Kubernetes API error."

def _follow_pod_log(
    pod:      client.V1Pod,
    previous: bool,
) -> Generator[Tuple[str, str], None, None]:
    core_api: client.CoreV1Api = init_core_api()
    try:
        # Equivalent to kubectl logs --tail=100 -f (or --previous, whose container has finished, so its log just ends).
        response = core_api.read_namespaced_pod_log(
            name             = pod.metadata.name,
            namespace        = pod.metadata.namespace,
            container        = pod.spec.containers[0].name,
            tail_lines       = _LOG_TAIL_LINES,
            previous         = previous,
            follow           = not previous,
            _preload_content = False,
            _request_timeout = (_LOG_CONNECT_TIMEOUT_SECONDS, _LOG_IDLE_SECONDS),
        )
    except ApiException as e:
        # e.g. 400 for a container still waiting to start (image pull, crash back-off): pass the reason on.
        yield ("unavailable", _api_message(e))
        return
    except HTTPError:
        yield ("unavailable", "Could not reach Kubernetes API.")
        return
    try:
        for line in _log_lines(response.stream(4096)):
            yield ("line", line)
    except (ReadTimeoutError, ProtocolError):
        pass  # Silent for _LOG_IDLE_SECONDS, or the connection dropped: end here and let the browser reconnect.
    finally:
        response.close()

def pod_log_events(
    spec:     NamespaceAndUid,
    pod_name: str,
    previous: bool,
) -> Generator[Tuple[str, str], None, None]:
    """
    Tail one pod's log: yields ("line", text) per log line, or ("unavailable", reason) when Kubernetes has no log to give.
    """

    # Raises NotFoundError / PodNotFoundError now, before any streaming starts, so they still become 404 responses.
    deployment: client.V1Deployment = _read_managed_deployment(spec.namespace, spec.uid)
    pod: Optional[client.V1Pod] = next(
        (p for p in _owned_pods(deployment) if p.metadata.name == pod_name),
        None,
    )
    if (pod is None):
        raise PodNotFoundError(
            pod = pod_name,
            uid = spec.uid,
        )
    return _follow_pod_log(pod, previous)

def _compute_status(
    deployment: client.V1Deployment,
    pods:       List[client.V1Pod],
) -> str:
    """
    •	Aggregated health: compute an overall status enum for each deployment: HEALTHY (all replicas ready), DEGRADED (some but not all ready), FAILING (no replicas ready or CrashLoopBackOff detected), PROGRESSING (rollout in progress), UNKNOWN.

    `pods` are the pods owned by the deployment. Checks run in order and the first match wins.
    """

    status: client.V1DeploymentStatus = deployment.status or client.V1DeploymentStatus()
    desired: int = deployment.spec.replicas
    ready:   int = status.ready_replicas or 0
    updated: int = status.updated_replicas or 0
    total:   int = status.replicas or 0
    progressing: Optional[client.V1DeploymentCondition] = next(
        (c for c in (status.conditions or []) if c.type == "Progressing"),
        None,
    )

    # 1. UNKNOWN: the controller has not observed the latest spec yet, so the status still describes an older one.
    if (status.observed_generation is None) or (status.observed_generation < deployment.metadata.generation):
        return "UNKNOWN"

    # 2. FAILING, hard failures: the rollout missed its progress deadline (600 s by default) ...
    # https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#failed-deployment
    if (progressing is not None) and (progressing.reason == "ProgressDeadlineExceeded"):
        return "FAILING"

    # ... or a live pod cannot be scheduled, or one of its containers cannot pull its image or keeps crashing.
    for pod in pods:
        if (pod.metadata.deletion_timestamp is not None):
            continue  # Terminating pods are on their way out.
        for condition in (pod.status.conditions or []):
            if (condition.type == "PodScheduled") and (condition.status == "False") and (condition.reason == "Unschedulable"):
                return "FAILING"
        for cs in (pod.status.container_statuses or []):
            if (cs.state.waiting is not None) and (cs.state.waiting.reason in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull")):
                return "FAILING"

    # 3. PROGRESSING: the controller has not marked the rollout complete (NewReplicaSetAvailable),
    #    not every pod runs the new template yet, or old pods are still around (as `kubectl rollout status` checks).
    rollout_complete: bool = (progressing is not None) and (progressing.reason == "NewReplicaSetAvailable")
    if (not rollout_complete) or (updated < desired) or (total > updated):
        return "PROGRESSING"

    # 4. FAILING: replicas are wanted but none is ready.
    if (desired > 0) and (ready == 0):
        return "FAILING"

    # 5. DEGRADED: some but not all replicas are ready.
    if (ready < desired):
        return "DEGRADED"

    # 6. HEALTHY: all replicas are ready (also when 0 are desired).
    return "HEALTHY"
