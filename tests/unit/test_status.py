# Standard imports.
from datetime import (
    datetime,
    timezone,
)
from typing import (
    List,
    Optional,
)

# Third-party imports.
import pytest
from kubernetes import client

# Local imports.
from app.k8s.services.deployments import _compute_status

# ==================== Builders ====================

def _deployment(
    desired:             int = 3,
    ready:               int = 3,
    updated:             int = 3,
    total:               int = 3,
    progressing_reason:  str = "NewReplicaSetAvailable",
    generation:          int = 1,
    observed_generation: Optional[int] = 1,
) -> client.V1Deployment:
    # A Deployment with just the fields the health logic reads. The defaults describe a finished rollout of 3 ready replicas.
    progressing: client.V1DeploymentCondition = client.V1DeploymentCondition(
        type   = "Progressing",
        status = "False" if (progressing_reason == "ProgressDeadlineExceeded") else "True",
        reason = progressing_reason,
    )
    return client.V1Deployment(
        metadata = client.V1ObjectMeta(name="web", generation=generation),
        spec     = client.V1DeploymentSpec(
            replicas = desired,
            selector = client.V1LabelSelector(),
            template = client.V1PodTemplateSpec(),
        ),
        status   = client.V1DeploymentStatus(
            observed_generation = observed_generation,
            replicas            = total,
            updated_replicas    = updated,
            ready_replicas      = ready,
            conditions          = [progressing],
        ),
    )

def _pod(
    waiting_reason: Optional[str] = None,
    unschedulable:  bool = False,
    terminating:    bool = False,
) -> client.V1Pod:
    # A Pod whose container may be waiting (e.g. CrashLoopBackOff), or which the scheduler could not place.
    container_statuses: List[client.V1ContainerStatus] = []
    if (waiting_reason is not None):
        container_statuses.append(client.V1ContainerStatus(
            name          = "web",
            image         = "nginx",
            image_id      = "",
            ready         = False,
            restart_count = 0,
            state         = client.V1ContainerState(
                waiting = client.V1ContainerStateWaiting(reason=waiting_reason),
            ),
        ))
    conditions: List[client.V1PodCondition] = []
    if unschedulable:
        conditions.append(client.V1PodCondition(type="PodScheduled", status="False", reason="Unschedulable"))
    return client.V1Pod(
        metadata = client.V1ObjectMeta(
            name               = "web-pod",
            deletion_timestamp = datetime.now(timezone.utc) if terminating else None,
        ),
        status   = client.V1PodStatus(
            phase              = "Pending" if unschedulable else "Running",
            conditions         = conditions,
            container_statuses = container_statuses,
        ),
    )

# ==================== Status mapping ====================

@pytest.mark.parametrize(("deployment", "pods", "expected"), [

    # HEALTHY: all replicas ready.
    pytest.param(_deployment(), [], "HEALTHY", id="all-replicas-ready"),
    pytest.param(_deployment(desired=0, ready=0, updated=0, total=0), [], "HEALTHY", id="scaled-to-zero"),
    pytest.param(_deployment(), [_pod("CrashLoopBackOff", terminating=True)], "HEALTHY", id="terminating-pod-is-ignored"),

    # DEGRADED: some but not all replicas ready.
    pytest.param(_deployment(ready=2), [], "DEGRADED", id="some-replicas-ready"),

    # FAILING: no replicas ready, or a pod cannot run.
    pytest.param(_deployment(ready=0), [], "FAILING", id="no-replicas-ready"),
    pytest.param(_deployment(ready=2), [_pod("CrashLoopBackOff")], "FAILING", id="crash-loop"),
    pytest.param(
        _deployment(ready=3, updated=1, total=4, progressing_reason="ReplicaSetUpdated"),
        [_pod("ImagePullBackOff")],
        "FAILING",
        id="bad-image-while-old-pods-serve",
    ),
    pytest.param(_deployment(ready=0, progressing_reason="ReplicaSetUpdated"), [_pod("ErrImagePull")], "FAILING", id="image-pull-error"),
    pytest.param(_deployment(ready=0, progressing_reason="ReplicaSetUpdated"), [_pod(unschedulable=True)], "FAILING", id="unschedulable-pod"),
    pytest.param(
        _deployment(ready=3, updated=1, total=4, progressing_reason="ProgressDeadlineExceeded"),
        [],
        "FAILING",
        id="progress-deadline-exceeded",
    ),

    # PROGRESSING: rollout in progress.
    pytest.param(
        _deployment(ready=0, progressing_reason="ReplicaSetUpdated"),
        [_pod("ContainerCreating")],
        "PROGRESSING",
        id="new-pods-starting",
    ),
    pytest.param(
        _deployment(ready=3, updated=1, total=4, progressing_reason="ReplicaSetUpdated"),
        [],
        "PROGRESSING",
        id="old-pods-being-replaced",
    ),

    # UNKNOWN: the controller has not reported on the latest spec yet.
    pytest.param(_deployment(observed_generation=None), [], "UNKNOWN", id="just-created"),
    pytest.param(_deployment(generation=2, observed_generation=1), [], "UNKNOWN", id="spec-change-not-observed"),
    pytest.param(
        _deployment(generation=2, observed_generation=1),
        [_pod("CrashLoopBackOff")],
        "UNKNOWN",
        id="unknown-wins-over-failing",
    ),
])
def test_status_mapping(
    deployment: client.V1Deployment,
    pods:       List[client.V1Pod],
    expected:   str,
):
    assert (_compute_status(deployment, pods) == expected)
