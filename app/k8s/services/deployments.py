# Standard imports.
from typing import (
    Any,
    Dict,
    List,
    Optional,
)
from datetime import datetime

# Third-party imports.
from kubernetes import client
from kubernetes.client.rest import ApiException
from app.exceptions import NotFoundError

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
)
    
def _namespace_exists(namespace: str) -> bool:
    try:
        core_api: client.CoreV1Api = init_core_api()
        core_api.read_namespace(namespace)
        return True # No error raised, namespace exists.
    except ApiException as e:
        if (e.status == 404):
            return False
        else:
            raise

def _create_namespace(namespace: str) -> None:
    core_api: client.CoreV1Api = init_core_api()
    core_api.create_namespace(
        client.V1Namespace(
            metadata = client.V1ObjectMeta(
                name = namespace,
            )
        )
    )

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

def create_deployment(deployment_spec: DeploymentSpec) -> Dict[str, Any]:

    # Access the namespace, ensure it exists.
    namespace: str = deployment_spec.namespace
    if not _namespace_exists(namespace):
        _create_namespace(namespace)

    # Build the Deployments object as a Dict object.
    body: Dict[str, Any] = _build_deployment(deployment_spec)

    # Create the object.
    apps_api: client.AppsV1Api = init_apps_api()
    response = apps_api.create_namespaced_deployment(
        body      = body,
        namespace = deployment_spec.namespace,
    )

    # Return details in dict.
    return {
        "uid": response.metadata.uid,
        # Just created: no pods exist yet and the controller has not observed it, so this is UNKNOWN.
        "status": _compute_status(response, []),
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
