# Standard imports.
import json
import subprocess
import textwrap
import time
import uuid
from collections import Counter
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
)

# Third-party imports.
import requests

BASE_URL: str = "http://127.0.0.1:5000/api/deployments"

# ==================== Settings and payloads (tweak these) ====================

# A namespace the API creates, so it gets the CPU/memory quota from quota.yaml (1 CPU and 2Gi of requests).
NAMESPACE: str = "failure-demo"

# Small pod requests: every failure below is about its scenario, never about the quota by accident.
REQUESTS: Dict[str, str] = {"cpu": "50m", "memory": "64Mi"}

# A valid request; each Part A case breaks exactly one field of it.
BASE_PAYLOAD: Dict[str, Any] = {
    "name":      "invalid-demo",
    "namespace": NAMESPACE,
    "image":     "nginx:latest",
    "replicas":  1,
    "resources": {"requests": REQUESTS},
}

def _with(**changes: Any) -> Dict[str, Any]:
    return {**BASE_PAYLOAD, **changes}

def _without(key: str) -> Dict[str, Any]:
    return {k: v for (k, v) in BASE_PAYLOAD.items() if k != key}

INVALID_CASES: List[Dict[str, Any]] = [
    {
        "title":   "Bad image format",
        "why":     "Image references are lowercase, so `NGINX:latest` is not a valid image.",
        "change":  '"image": "NGINX:latest"',
        "payload": _with(image="NGINX:latest"),
    },
    {
        "title":   "Negative replicas",
        "why":     "A replica count cannot be below 0.",
        "change":  '"replicas": -1',
        "payload": _with(replicas=-1),
    },
    {
        "title":   "Invalid resource value",
        "why":     "`lots` is not a Kubernetes quantity (valid ones look like `100m`, `0.5` or `64Mi`).",
        "change":  '"resources": {"requests": {"cpu": "lots", ...}}',
        "payload": _with(resources={"requests": {"cpu": "lots", "memory": "64Mi"}}),
    },
    {
        "title":   "DNS-incompatible name",
        "why":     "Names become DNS names: lowercase letters, digits and '-', starting with a letter.",
        "change":  '"name": "Web_App"',
        "payload": _with(name="Web_App"),
    },
    {
        "title":   "Missing required field",
        "why":     "`image` is required, and this request leaves it out.",
        "change":  '"image" removed',
        "payload": _without("image"),
    },
]

# Part A: created once, then created again (409) and scaled past the quota (403).
DUPLICATE_PAYLOAD: Dict[str, Any] = _with(name="duplicate")

# 30 pods x 50m = 1.5 CPU, more than the namespace's 1 CPU quota.
QUOTA_REPLICAS: int = 30

# Part B: the healthy baseline, nginx x3. Its readiness check starts 10 s after a container starts,
# so the in-between states (PROGRESSING, DEGRADED, FAILING) stay on screen long enough to read.
WEB_PAYLOAD: Dict[str, Any] = {
    "name":      "web",
    "namespace": NAMESPACE,
    "image":     "nginx:latest",
    "replicas":  3,
    "port":      80,
    "resources": {"requests": REQUESTS},
    "readiness_probe": {
        "http_get":              {"port": 80, "path": "/"},
        "initial_delay_seconds": 10,
        "period_seconds":        2,
    },
}

# Part B: the image tag that does not exist (TASK.md's suggested scenario).
BAD_IMAGE: str = "nginx:nonexistent"

# Part B: a container that prints an error and exits at once, every time it is started.
# Like a real web app it has a readiness check on its port; without one, Kubernetes would count the container
# as ready for the split second it runs before crashing.
CRASHER_PAYLOAD: Dict[str, Any] = {
    "name":      "crasher",
    "namespace": NAMESPACE,
    "image":     "busybox",
    "replicas":  1,
    "command":   ["sh", "-c", "echo 'Starting up...'; echo 'Fatal: missing config file /etc/app.conf'; exit 1"],
    "resources": {"requests": REQUESTS},
    "readiness_probe": {
        "http_get": {"port": 8080, "path": "/healthz"},
    },
}

# Part B: asks for 64 CPUs. In `default`, which has no quota (the API did not create it), so Kubernetes decides.
TOO_BIG_PAYLOAD: Dict[str, Any] = {
    "name":      "too-big",
    "namespace": "default",
    "image":     "nginx:latest",
    "replicas":  1,
    "resources": {"requests": {"cpu": "64", "memory": "64Mi"}},
}

# How often the status timeline polls the API, and how long it waits for a state before giving up.
POLL_SECONDS: int = 1
WAIT_SECONDS: int = 120

HEALTH_RULES: str = """\
How the API computes a deployment's health, from live Deployment and Pod status (checked in this order,
the first rule that matches wins):

    UNKNOWN      Kubernetes has not processed the latest change yet (status.observedGeneration < generation).
    FAILING      A pod cannot be scheduled, cannot pull its image or keeps crashing (CrashLoopBackOff),
                 or the rollout ran past its progress deadline.
    PROGRESSING  A rollout is still under way: new pods are not all up yet, or old pods are still around.
    FAILING      Replicas are wanted, but none of them is ready.
    DEGRADED     Some, but not all, replicas are ready.
    HEALTHY      Every replica is ready.
"""

# ==================== Helpers ====================

def _wait() -> None:
    input("")

def _separator() -> None:
    print("-"*100 + '\n')

def _kubectl(*commands: str) -> None:
    print("\nRun this command to check this on your actual cluster:" if len(commands) == 1
          else "\nRun these commands to check this on your actual cluster:")
    for command in commands:
        print(f"    {command}")

def _web_page(text: str) -> None:
    print(f"\nIn the web page (http://127.0.0.1:5000): {text}")

def _run(command: List[str]) -> None:
    # Demo-only fault injection (deleting pods), not part of the API.
    print(f"    $ {' '.join(command)}")
    subprocess.run(command, capture_output=True)

def _cleanup() -> None:
    # Demo housekeeping, not part of the API: the namespace, and the one deployment that lives in `default`.
    # kubectl waits until the namespace is gone, so a rerun never meets a namespace that is still terminating.
    commands: List[List[str]] = [
        ["kubectl", "delete", "deployment", TOO_BIG_PAYLOAD["name"], "-n", TOO_BIG_PAYLOAD["namespace"], "--ignore-not-found"],
        ["kubectl", "delete", "namespace", NAMESPACE, "--ignore-not-found"],
    ]
    try:
        for command in commands:
            print(f"    {' '.join(command)}")
            subprocess.run(command, capture_output=True)
    except FileNotFoundError:
        print("kubectl not found; run the commands above yourself.")

def _pod_state(pod: Dict[str, Any]) -> str:
    # One word per pod: its container's state, or the pod phase while no container exists yet.
    containers: List[Dict[str, Any]] = pod.get("containers") or []
    if not containers:
        return pod.get("phase") or "Pending"
    state: Dict[str, Any] = containers[0].get("state") or {}
    if "running" in state:
        return "Running" if containers[0].get("ready") else "Running (not ready)"
    if "waiting" in state:
        return (state["waiting"] or {}).get("reason") or "Waiting"
    if "terminated" in state:
        return (state["terminated"] or {}).get("reason") or "Terminated"
    return pod.get("phase") or "Unknown"

def _pod_message(pod: Dict[str, Any]) -> str:
    # Kubernetes' explanation of a waiting or terminated container, when it gives one.
    containers: List[Dict[str, Any]] = pod.get("containers") or []
    state: Dict[str, Any] = (containers[0].get("state") or {}) if containers else {}
    detail: Dict[str, Any] = state.get("waiting") or state.get("terminated") or {}
    return detail.get("message") or ""

def _status_line(detail: Dict[str, Any]) -> str:
    replicas: Dict[str, Any] = detail.get("replicas") or {}
    counts = Counter(_pod_state(pod) for pod in detail.get("pods") or [])
    pods: str = ", ".join(f"{n} {state}" for (state, n) in sorted(counts.items())) or "none yet"
    rollout: str = next(
        (c.get("reason") for c in detail.get("conditions") or [] if c.get("type") == "Progressing"),
        None,
    ) or "not started"
    return (
        f"{detail.get('status', '?'):<11}  ready {replicas.get('ready', 0)}/{replicas.get('desired', 0)}   "
        f"pods: {pods:<44}  rollout: {rollout}"
    )

def _timeline(
    uid:        str,
    target:     str,
    must_leave: bool = False,
    also:       Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> Dict[str, Any]:
    # Polls GET /api/deployments/<uid> and prints a line whenever the status, replica counts or pod states change,
    # until the status is `target` (after first moving away from it, with must_leave) and `also` holds.
    print("\nLive status, polled from the API every second (a line appears whenever something changes):")
    start: float = time.monotonic()
    left: bool = not must_leave
    last: Optional[str] = None
    while True:
        detail: Dict[str, Any] = requests.get(f"{BASE_URL}/{uid}").json()
        line: str = _status_line(detail)
        if (line != last):
            print(f"    [+{time.monotonic() - start:>3.0f}s] {line}")
            last = line
        if (detail.get("status") != target):
            left = True
        elif left and ((also is None) or also(detail)):
            return detail
        if (time.monotonic() - start > WAIT_SECONDS):
            print(f"    (stopped waiting after {WAIT_SECONDS} s)")
            return detail
        time.sleep(POLL_SECONDS)

def _show_levels(detail: Dict[str, Any]) -> None:
    # What GET /api/deployments/<uid> reports at the deployment level and for each pod.
    replicas: Dict[str, Any] = detail.get("replicas") or {}
    print("\nWhat the API reports at the deployment level:")
    print(
        f"    status {detail.get('status')}: desired {replicas.get('desired')}, ready {replicas.get('ready')}, "
        f"available {replicas.get('available')}, unavailable {replicas.get('unavailable')}"
    )
    for condition in detail.get("conditions") or []:
        print(f"    condition {condition.get('type')}={condition.get('status')} ({condition.get('reason')}): {condition.get('message')}")
    print("\n...and for each pod:")
    for pod in sorted(detail.get("pods") or [], key=lambda p: p["name"]):
        print(
            f"    {pod['name']}: phase {pod.get('phase')}, restarts {pod.get('restartCount')}, "
            f"node {pod.get('node') or '(not scheduled)'}, container state {_pod_state(pod)}"
        )
        message: str = _pod_message(pod)
        if message:
            print(f"        {message[:200]}")

def _create(payload: Dict[str, Any]) -> Dict[str, Any]:
    response = requests.post(
        url  = BASE_URL,
        json = payload,
    )
    data: Dict[str, Any] = response.json()
    print(f"Server Responded With Status Code {response.status_code}")
    print("The Response Body is:")
    print(json.dumps(data, indent=2, default=str))
    return data

# ==================== Part A: requests the API refuses ====================

def invalid_input() -> None:

    _separator()
    print("Failure Mode: Invalid Input.")
    print(
        "Description (as provided in document): return 400 for invalid input "
        "(bad image format, negative replicas, invalid resource values, DNS-incompatible names), "
        "and for missing required fields.\n"
    )
    print()
    print(
        "Each request below has a valid payload with exactly one field invalid. " 
        "The API validates the request body before sending any request to the Kubernetes API. "
        "So in each of these cases, no deployment actually gets created. "
    )
    print()
    print("In each of these invalid input cases, the base payload is:")
    print(json.dumps(WEB_PAYLOAD))

    print(f"\n- Press ENTER to continue.")
    _wait()

    for (number, case) in enumerate(INVALID_CASES, start=1):
        
        _separator()

        response = requests.post(
            url  = BASE_URL,
            json = case["payload"],
        )
        data: Dict[str, Any] = response.json()

        print(f"{number}. {case['title']}: {case['why']}")
        print(f"   Change from the base payload: {case['change']}")
        print("\n- Press ENTER to run this request.")
        _wait()
        print(f"   Server Responded With Status Code {response.status_code}:")
        print(json.dumps(data, indent=2, default=str))
        print()
        print(f"\n- Press ENTER to continue to next invalid input request.")
        _wait()

    print("\n- Press ENTER to continue to the next failure mode.\n")
    _wait()

def unknown_id() -> None:

    _separator()
    print("Failure Mode: Unknown Deployment ID.")
    print(
        "Description (as provided in document): return 404 when a deployment ID doesn't exist.\n"
        "Deployments are identified by their Kubernetes UID. This one is a well-formed UID that no deployment has."
    )
    missing: str = str(uuid.uuid4())

    print(f"\n- Press ENTER to request GET /api/deployments/{missing}")
    _wait()

    response = requests.get(f"{BASE_URL}/{missing}")
    print(f"Server Responded With Status Code {response.status_code}")
    print("The Response Body is:")
    print(json.dumps(response.json(), indent=2))

    print("\n- Press ENTER to continue to the next failure.\n")
    _wait()

def duplicate_name() -> Optional[str]:

    _separator()
    print("Part A, failure 3: duplicate name -> 409")
    print(
        "Description (as provided in document): return 409 when creating a deployment with a name that already "
        f"exists in the same namespace.\nWe create `{DUPLICATE_PAYLOAD['name']}` in `{NAMESPACE}`, then send the exact same request again."
    )
    print("\nThe request payload is:")
    print(json.dumps(DUPLICATE_PAYLOAD, indent=2))

    print("\n- Press ENTER to create it the first time.")
    _wait()
    data: Dict[str, Any] = _create(DUPLICATE_PAYLOAD)

    print("\n- Press ENTER to send the same request again.")
    _wait()
    _create(DUPLICATE_PAYLOAD)

    _kubectl(f"kubectl get deployment {DUPLICATE_PAYLOAD['name']} -n {NAMESPACE}")

    print("\n- Press ENTER to continue to the next failure.\n")
    _wait()

    return data.get("uid")

def quota_exceeded(uid: Optional[str]) -> None:

    _separator()
    print("Part A, failure 4: not enough quota -> 403 (stretch goal: resource quotas)")
    print(
        "Every namespace the API creates gets a ResourceQuota from quota.yaml (1 CPU and 2Gi of requests here). "
        "Kubernetes enforces a quota pod by pod: it would accept an oversized Deployment and then quietly refuse "
        "its pods. So the API checks first and refuses the request itself, with Kubernetes' own wording.\n"
        f"Two attempts, each needing {QUOTA_REPLICAS} pods x {REQUESTS['cpu']} = more than 1 CPU: creating a new deployment, "
        f"and scaling `{DUPLICATE_PAYLOAD['name']}` up."
    )

    print("\n- Press ENTER to try creating the oversized deployment.")
    _wait()
    _create(_with(name="too-many", replicas=QUOTA_REPLICAS))

    if uid:
        print(f"\n- Press ENTER to try scaling `{DUPLICATE_PAYLOAD['name']}` to {QUOTA_REPLICAS} replicas.")
        _wait()
        response = requests.patch(
            url  = f"{BASE_URL}/{uid}",
            json = {"replicas": QUOTA_REPLICAS},
        )
        print(f"Server Responded With Status Code {response.status_code}")
        print("The Response Body is:")
        print(json.dumps(response.json(), indent=2))

    _kubectl(
        f"kubectl get resourcequota -n {NAMESPACE}",
        f"kubectl get deployments -n {NAMESPACE}    (no `too-many`, and `{DUPLICATE_PAYLOAD['name']}` still has 1 replica)",
    )

    print(
        "\nOne more error the API maps: if the Kubernetes API server cannot be reached, requests get "
        "503 K8S_UNREACHABLE. It is not shown live, because that means stopping the cluster."
    )

    print("\n- Press ENTER to continue to Part B.\n")
    _wait()

# ==================== Part B: deployments that break on the cluster ====================

def healthy_rollout() -> Optional[str]:

    _separator()
    print("Part B, status 1: UNKNOWN -> PROGRESSING -> HEALTHY")
    print(HEALTH_RULES)
    print(
        "First, the baseline: nginx with 3 replicas. The POST response says UNKNOWN, because Kubernetes has not "
        "processed the new Deployment yet. It is PROGRESSING while the pods start (their readiness check waits "
        "10 s), and HEALTHY once all 3 are ready."
    )
    print("\nThe request payload is:")
    print(json.dumps(WEB_PAYLOAD, indent=2))

    print("\n- Press ENTER to create it and follow its status.")
    _wait()
    data: Dict[str, Any] = _create(WEB_PAYLOAD)
    uid: Optional[str] = data.get("uid")
    if uid:
        _timeline(uid, "HEALTHY")

    _web_page("the dashboard shows `web` turning from yellow (PROGRESSING) to green (HEALTHY).")
    _kubectl(f"kubectl get deployment {WEB_PAYLOAD['name']} -n {NAMESPACE}")

    print("\n- Press ENTER to continue to the next status.\n")
    _wait()

    return uid

def degraded(uid: str) -> None:

    _separator()
    print("Part B, status 2: DEGRADED (some, but not all, replicas ready)")
    print(
        "We delete one of the three pods, as if it had crashed. Kubernetes starts a replacement straight away, "
        "but the replacement is not ready for about 10 s. Meanwhile 2 of 3 replicas are ready: DEGRADED. "
        "Then it recovers to HEALTHY on its own.\n"
        "(The deleted pod is still listed for a moment while it shuts down, so the pod counts briefly add up to 4.)"
    )

    print("\n- Press ENTER to delete one pod and follow the status.")
    _wait()
    pods: List[Dict[str, Any]] = requests.get(f"{BASE_URL}/{uid}").json().get("pods") or []
    victim: str = sorted(pod["name"] for pod in pods)[0]
    _run(["kubectl", "delete", "pod", victim, "-n", NAMESPACE, "--wait=false"])
    _timeline(uid, "HEALTHY", must_leave=True)

    _web_page("`web` briefly turns yellow (DEGRADED) with 2/3 ready, then green again.")
    _kubectl(f"kubectl get pods -n {NAMESPACE} -l app.kubernetes.io/name={WEB_PAYLOAD['name']}")

    print("\n- Press ENTER to continue to the next status.\n")
    _wait()

def no_replicas_ready(uid: str) -> None:

    _separator()
    print("Part B, status 3: FAILING (no replicas ready)")
    print(
        "This time all three pods go at once. Until their replacements are ready, none of the replicas the "
        "Deployment wants is ready: FAILING. It recovers on its own, through DEGRADED, back to HEALTHY.\n"
        "(The deleted pods are still listed for a moment while they shut down.)"
    )

    print("\n- Press ENTER to delete all three pods and follow the status.")
    _wait()
    _run(["kubectl", "delete", "pods", "-n", NAMESPACE, "-l", f"app.kubernetes.io/name={WEB_PAYLOAD['name']}", "--wait=false"])
    _timeline(uid, "HEALTHY", must_leave=True)

    _web_page("`web` turns red (FAILING) with 0/3 ready, then green again.")
    _kubectl(f"kubectl get pods -n {NAMESPACE} -l app.kubernetes.io/name={WEB_PAYLOAD['name']}")

    print("\n- Press ENTER to continue to the next status.\n")
    _wait()

def bad_image(uid: str) -> None:

    _separator()
    print("Part B, status 4: FAILING (ImagePullBackOff), the scenario suggested in the document")
    print(
        f"Description (as provided in document): deploy a deliberately bad image (e.g., {BAD_IMAGE}) and verify "
        "your API correctly reports the failure state (ImagePullBackOff) at both the deployment and pod level.\n"
        f"We change `web`'s image to `{BAD_IMAGE}`. Kubernetes starts one new pod with it first and keeps the "
        "3 old pods serving until the new one is ready, which never happens. Kubernetes itself would only call "
        "this rollout failed after its 10-minute progress deadline; the API reports FAILING as soon as a pod "
        "cannot pull its image."
    )

    print("\n- Press ENTER to send the image update and follow the status.")
    _wait()
    response = requests.patch(
        url  = f"{BASE_URL}/{uid}",
        json = {"image": BAD_IMAGE},
    )
    print(f"Server Responded With Status Code {response.status_code} (the PATCH itself is valid)")
    detail: Dict[str, Any] = _timeline(
        uid, "FAILING",
        also = lambda d: any(_pod_state(pod) == "ImagePullBackOff" for pod in d.get("pods") or []),
    )
    _show_levels(detail)

    _web_page("`web` is red (FAILING). In its detail view, the new pod shows ImagePullBackOff, and the "
              "Logs panel for that pod explains that the image can't be pulled.")
    _kubectl(
        f"kubectl get pods -n {NAMESPACE} -l app.kubernetes.io/name={WEB_PAYLOAD['name']}",
        f"kubectl describe deployment {WEB_PAYLOAD['name']} -n {NAMESPACE}",
    )

    print("\n- Press ENTER to continue to the next status.\n")
    _wait()

def crash_loop() -> None:

    _separator()
    print("Part B, status 5: FAILING (CrashLoopBackOff)")
    print(
        "Description (as provided in document): FAILING when no replicas are ready or CrashLoopBackOff is detected.\n"
        "A container that prints an error and exits every time it starts. Kubernetes restarts it, waiting longer "
        "after each crash (10 s, 20 s, 40 s, ...); that waiting state is CrashLoopBackOff."
    )
    print("\nThe request payload is:")
    print(json.dumps(CRASHER_PAYLOAD, indent=2))

    print("\n- Press ENTER to create it and follow its status.")
    _wait()
    data: Dict[str, Any] = _create(CRASHER_PAYLOAD)
    uid: Optional[str] = data.get("uid")
    if uid:
        detail: Dict[str, Any] = _timeline(
            uid, "FAILING",
            also = lambda d: any(_pod_state(pod) == "CrashLoopBackOff" for pod in d.get("pods") or []),
        )
        _show_levels(detail)

    _web_page("`crasher` is red (FAILING). In its Logs panel, tick 'Previous container' to read why it crashed.")
    _kubectl(
        f"kubectl get pods -n {NAMESPACE} -l app.kubernetes.io/name={CRASHER_PAYLOAD['name']}",
        f"kubectl logs deployment/{CRASHER_PAYLOAD['name']} -n {NAMESPACE} --previous",
    )

    print("\n- Press ENTER to continue to the next status.\n")
    _wait()

def unschedulable() -> None:

    _separator()
    print("Part B, status 6: FAILING (pod cannot be scheduled: insufficient resources)")
    print(
        f"A pod that asks for {TOO_BIG_PAYLOAD['resources']['requests']['cpu']} CPUs, more than the node has. "
        "In a namespace the API created, the quota would refuse this with a 403 (Part A). This one goes to "
        f"`{TOO_BIG_PAYLOAD['namespace']}`, which existed before and has no quota, so Kubernetes itself decides: it "
        "accepts the Deployment, but its scheduler cannot place the pod, which stays Pending."
    )
    print("\nThe request payload is:")
    print(json.dumps(TOO_BIG_PAYLOAD, indent=2))

    print("\n- Press ENTER to create it and follow its status.")
    _wait()
    data: Dict[str, Any] = _create(TOO_BIG_PAYLOAD)
    uid: Optional[str] = data.get("uid")
    if uid:
        detail: Dict[str, Any] = _timeline(
            uid, "FAILING",
            also = lambda d: any(pod.get("phase") == "Pending" for pod in d.get("pods") or []),
        )
        _show_levels(detail)

    _kubectl(
        f"kubectl get pods -n {TOO_BIG_PAYLOAD['namespace']} -l app.kubernetes.io/name={TOO_BIG_PAYLOAD['name']}",
        f"kubectl describe pods -n {TOO_BIG_PAYLOAD['namespace']} -l app.kubernetes.io/name={TOO_BIG_PAYLOAD['name']}    "
        "(Events: 'Insufficient cpu')",
    )

    print("\n- Press ENTER to end the demo.\n")
    _wait()

if (__name__ == "__main__"):
    try:

        print(
            "\nThis demo shows how the API fails, on purpose, in two parts:\n"
            "  Part A: requests the API refuses, and the structured errors it returns (400, 404, 409, 403).\n"
            "  Part B: deployments that break on the cluster, and how the API's health status (computed from live\n"
            "          Deployment and Pod status, never cached) reports each one: UNKNOWN, PROGRESSING, HEALTHY,\n"
            "          DEGRADED and FAILING.\n"
            f"Everything runs in the namespace `{NAMESPACE}` (plus one deployment in `default`), and is deleted\n"
            "when the demo ends, even if you stop it with Ctrl+C.\n"
            "Keep the web page (http://127.0.0.1:5000) open next to this terminal to watch the colours change."
        )
        print("\nFirst, removing anything left over from an earlier run:")
        _cleanup()
        print("\nPress ENTER when you're ready to start.")
        _wait()

        invalid_input()
        unknown_id()
        duplicate_uid: Optional[str] = duplicate_name()
        quota_exceeded(duplicate_uid)

        web_uid: Optional[str] = healthy_rollout()
        if (web_uid is None):
            raise SystemExit("Creating `web` failed; check the API and run again.")
        degraded(web_uid)
        no_replicas_ready(web_uid)
        bad_image(web_uid)
        crash_loop()
        unschedulable()

    except requests.ConnectionError:
        raise SystemExit("Cannot reach the API. Start it first: venv/bin/python run.py")
    except KeyboardInterrupt:
        print("\nDemo interrupted.")
    finally:
        print("\nCleaning up (this takes a few seconds):")
        _cleanup()
