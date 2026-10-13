# Standard imports.
import json
import subprocess
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
import pyperclip

BASE_URL: str = "http://127.0.0.1:5000/api/deployments"

# ==================== Payloads ====================

# A namespace the API creates.
NAMESPACE: str = "failure-demo"

# Small pod requests: every failure below is about its scenario, never about the quota by accident.
REQUESTS: Dict[str, str] = {
    "cpu": "50m",
    "memory": "64Mi",
}

# Used to test invalid inputs.
BASE_PAYLOAD: Dict[str, Any] = {
    "name":      "web",
    "namespace": NAMESPACE,
    "image":     "nginx:latest",
    "replicas":  1,
    "resources": {"requests": REQUESTS},
}

def _with(changes: Dict[str, Any]) -> Dict[str, Any]:
    return {**BASE_PAYLOAD, **changes}

def _without(keys: List[str]) -> Dict[str, Any]:
    return {k: v for (k, v) in BASE_PAYLOAD.items() if (k not in keys)}

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

BAD_IMAGE: str = "nginx:nonexistent"

CRASHER_PAYLOAD: Dict[str, Any] = {
    "name":      "crasher",
    "namespace": NAMESPACE,
    "image":     "busybox",
    "replicas":  1,
    "command":   ["sh", "-c", "echo 'Starting up...'; echo 'Fatal: missing config file /etc/app.conf'; exit 1"],
    "resources": {"requests": REQUESTS},
}

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

def _delete_namespace() -> None:
    # Demo housekeeping, not part of the API: removes everything this run created, even after Ctrl+C.
    # kubectl waits until the namespace is gone, so a rerun never meets a namespace that is still terminating.
    namespace: str = NAMESPACE
    if namespace in ("default", "kube-system", "kube-public", "kube-node-lease"):
        return
    command = ["kubectl", "delete", "namespace", namespace, "--ignore-not-found"]
    print(f"\nCleaning up: {' '.join(command)}  (this takes a few seconds)")
    try:
        subprocess.run(command)
    except FileNotFoundError:
        print(f"kubectl not found; delete the namespace yourself: {' '.join(command)}")

def _delete_too_big() -> None:
    # `too-big` lives in `default`, which must never be deleted, so it is removed on its own.
    command = [
        "kubectl", "delete", "deployment", TOO_BIG_PAYLOAD["name"],
        "-n", TOO_BIG_PAYLOAD["namespace"], "--ignore-not-found",
    ]
    print(f"Cleaning up: {' '.join(command)}")
    try:
        subprocess.run(command)
    except FileNotFoundError:
        print(f"kubectl not found; delete the deployment yourself: {' '.join(command)}")

# ==================== Functions To Run Failure Tests ====================

def invalid_input() -> None:

    _separator()
    print("FAILURE MODE: Invalid Input.")
    print(
        "Description (as provided in document): return 400 for invalid input "
        "(bad image format, negative replicas, invalid resource values, DNS-incompatible names), "
        "and for missing required fields.\n"
    )
    print()
    print(
        "This failure mode has 5 sub-parts, one for each example mentioned in the document: "
        "bad image format, negative replicas, invalid resource values, DNS-incompatible names."
    )
    print()
    print("In each of these invalid input cases, the base payload is:")
    print(json.dumps(BASE_PAYLOAD, indent=2, default=str))
    print()
    print("In each sub-part, we will (on purpose) override one field of this base payload and make it invalid.")
    print(
        "The API validates the request body before sending any request to the Kubernetes API. "
        "So we expect to get an appropriate error status code from the API with a message telling what field we got wrong."
    )

    print(f"\n- Press ENTER to continue.")
    _wait()

    INVALID_CASES: List[Dict[str, Any]] = [
        {
            "title":   "Bad image format",
            "label":    "`image` has been set to `NGINX:latest`",
            "why":     "Image references are lowercase, so `NGINX:latest` is not a valid image.",
            "change":  {
                "image": "NGINX:latest",
            },
        },
        {
            "title":   "Negative replicas",
            "label":    "`replicas` has been set to -1",
            "why":     "A replica count cannot be below 0.",
            "change":  {
                "replicas": -1,
            },
        },
        {
            "title":   "Invalid resource value",
            "label":    "`resources.cpu` has been set to `lots`",
            "why":     "`lots` is not a Kubernetes quantity (valid ones look like `100m`, `0.5` or `64Mi`).",
            "change":  {
                "resources": {
                    "requests": {
                        "cpu": "lots",
                        "memory": "64Mi",
                    }
                }
            },
        },
        {
            "title":   "DNS-incompatible name",
            "label":    "`name` has been set to `Our App`",
            "why":     "Names can only have: lowercase letters, digits and '-', starting with a letter.",
            "change":  {
                "name": "Our App",
            },
        },
        {
            "title":   "Missing required field",
            "label":    "`image` has been removed from the payload.",
            "why":     "`image` is required, and this request leaves it out.",
            "remove":  ["image"],
        },
    ]

    for (number, case) in enumerate(INVALID_CASES, start=1):
        
        _separator()

        print(f"Sub-Part {number}. {case['title']}")
        print(f"- What Changed: {case['label']}")
        print(f"- Why it should fail: {case['why']}")

        if ("change") in case:
            payload: Dict[str, Any] = _with(case['change'])
        if ("remove" in case):
            payload = _without(case["remove"])

        print("Our payload is set to:")
        print(json.dumps(payload, indent=2, default=str))

        print("\n- Press ENTER to run this request: POST /api/deployments")
        _wait()
        
        _create(payload)

        print()
        print(f"\n- Press ENTER to continue to next invalid input request.")
        _wait()

    print("\n- Press ENTER to continue to the next failure mode.\n")
    _wait()

def unknown_id() -> None:

    _separator()
    print("FAILURE MODE: Unknown Deployment ID.")
    print("Description (as provided in document): return 404 when a deployment ID doesn't exist.\n")

    missing: str = str(uuid.uuid4())
    print("- We have generated a random UUID which does not exist in our cluster.")
    print(f"\n- Press ENTER to run this request: GET /api/deployments/{missing}")
    _wait()

    response = requests.get(f"{BASE_URL}/{missing}")
    print(f"Server Responded With Status Code {response.status_code}")
    print("The Response Body is:")
    print(json.dumps(response.json(), indent=2, default=str))

    print("\n- Press ENTER to continue to the next failure mode.\n")
    _wait()

def duplicate_name() -> Optional[str]:

    _separator()
    print("FAILURE MODE: Duplication")
    print(
        "Description (as provided in document): return 409 when creating a deployment "
        "with a name that already exists in the same namespace."
    )
    print(
        f"\nWe will first create a deployment with name `test-deployment` "
        f"in namespace `{NAMESPACE}`, then send the exact same request again."
    )

    payload: Dict[str, Any] = _with({"name": "test-duplicate"})
    print("\nThe request payload (for both requests) is:")
    print(json.dumps(payload, indent=2))

    print("\n- Press ENTER to create it the first time.")
    _wait()
    data: Dict[str, Any] = _create(payload)

    print("\n- Press ENTER to send the same request again.")
    _wait()
    _create(payload)

    print("\n- Press ENTER to continue to the next failure mode.\n")
    _wait()

    return data.get("uid")

def quota_exceeded(uid: Optional[str]) -> None:

    _separator()
    print("FAILURE MODE: Resource Quota Exceeded (Part of Stretch Goal).")
    print(
        "Every namespace the API creates gets a ResourceQuota from quota.yaml (originally: 1 CPU and 2Gi of requests here). "
        "Kubernetes enforces a quota pod by pod: it would accept an oversized Deployment and then quietly refuse "
        "its pods. So our API checks first and if the deployment create/patch request would violate the ResourceQuota,"
        "it stright-up refuses the request without passing it to the Kubernetes API.\n"
    )
    print()

    print(
        "Let's first create a new deployment, and request resources which will exceed our ResourceQuota "
        " (set to: 1 CPU and 2Gi, unless edits were made to `quota.yaml`), and see what the API returns."
    )

    payload: Dict[str, Any] = _with({
        "name": "too-many-pods",
        "replicas": 30,
    })
    print("Our request payload is set to:")
    print(json.dumps(payload, indent=2, default=str))
    print(
        "Our request is set to have 30 pods, and since requests.resources apply on a per-pod level,"
        "we're asking for {(50m*30) = 1500m = 1.5 CPU}, which exceeds our set quota of 1 CPU."
    )
    
    print("\n- Press ENTER to try creating the oversized deployment.")
    _wait()
    
    _create(payload)

    if uid:
        payload: Dict[str, Any] = {"replicas": 40}
        print(
            "\nNow, let's try scaling our `test-deployment` "
            "(which we created in the duplication failure test)"
            "to 40 replicas. We will use our PATCH endpoint for this."
        )
        print("Our request payload is simply set to (that's enough for PATCH request to work):")
        print(json.dumps(payload))
        print("Press ENTER to run the PATCH /api/deployments request.")
        _wait()

        response = requests.patch(
            url  = f"{BASE_URL}/{uid}",
            json = {"replicas": 40},
        )
        print(f"Server Responded With Status Code {response.status_code}")
        print("The Response Body is:")
        print(json.dumps(response.json(), indent=2))

    print(
        "\nOne more error that is kinda hard to demo here: "
        "If the Kubernetes API server cannot be reached, requests return 503 status code."
        "You can test this by turning off minikube later, and sending an API request."
    )

    print("\n- Press ENTER to continue.\n")
    _wait()

def healthy_rollout() -> Optional[str]:

    _separator()
    print("STATUS TRACKING: For this part, we will track the status of a healthy rollout.")
    print("\nThe request payload is:")
    print(json.dumps(WEB_PAYLOAD, indent=2))

    print("\n- Press ENTER to create it and then follow its status in the UI.")
    _wait()
    data: Dict[str, Any] = _create(WEB_PAYLOAD)
    uid: Optional[str] = data.get("uid")

    print("\n- Press ENTER to continue to the next status test.\n")
    _wait()

    return uid

def degraded(uid: str) -> None:

    _separator()
    print("STATUS: DEGRADED i.e., some (but not all) replicas are ready.")
    print("To simulate a pod crashing, we will now suddenly delete one of the three pods (of the `web` deployment).")
    print("What we expect: Going from HEALTHY to DEGRADED back to HEALTHY.")

    pods: List[Dict[str, Any]] = requests.get(f"{BASE_URL}/{uid}").json().get("pods") or []
    victim: str = sorted(pod["name"] for pod in pods)[0]
    print()
    print("Run this command (already copied to your clipboard) and quickly switch to the UI:")
    print(f"kubectl delete pod {victim} -n {NAMESPACE} --wait=false")
    pyperclip.copy(f"kubectl delete pod {victim} -n {NAMESPACE} --wait=false")

    print("\n- Press ENTER to continue to the next status test.\n")
    _wait()

def no_replicas_ready(uid: str) -> None:

    _separator()
    print("STATUS: FAILING i.e., no replicas are ready.")
    print("To simulate ALL pods crashing, we will suddenly delete all the pods (of the `web` deployment).")
    print("What we expect: Going from HEALTHY to FAILING to DEGRADED back to HEALTHY.")
    print()
    print("Run this command (already copied to your clipboard) and quickly switch to the UI:")
    print(f"kubectl delete pods -n {NAMESPACE} -l app.kubernetes.io/name={WEB_PAYLOAD['name']} --wait=false")
    pyperclip.copy(f"kubectl delete pods -n {NAMESPACE} -l app.kubernetes.io/name={WEB_PAYLOAD['name']} --wait=false")

    print("\n- Press ENTER to continue to the next status.\n")
    _wait()

def bad_image(uid: str) -> None:

    _separator()
    print("STATUS: FAILING due to ImagePullBackOff.")
    print(f"We'll 'patch' our `web` deployment to a bad image ({BAD_IMAGE}).")
    print("What we expect:")
    print("- The document states to classify CrashLoopBackOff detection as FAILING; we do the same for ErrImagePull/ImagePullBackOff and for pods that can't be scheduled (Unschedulable).")
    print("- Initially, all 3 of our pods are running and ready, so we have a HEALTHY status for the deployment.")
    print("- Then, to roll out the patch update, Kubernetes starts 1 new pod with the new image on top of our 3 (maxSurge: 25% of 3, rounded up to 1) and keeps all 3 old pods running until the new one is ready (maxUnavailable: 25% of 3, rounded down to 0). For the few seconds the new pod is being created, the status is PROGRESSING. But the new pod never becomes ready, because its image can't be pulled: its container waits with ErrImagePull, then ImagePullBackOff.")
    print("- When ErrImagePull or ImagePullBackOff is detected in any of the deployment's pods, our deployment status changes to FAILING, even though the 3 old pods are still ready (3/3).")
    print("- Kubernetes itself keeps reporting the rollout as in progress (Progressing=True) until its 10-minute progress deadline passes; our status reports the failure straight away.")
    print("- In the UI, the deployment turns red, and its pod table shows the new pod's ImagePullBackOff next to the 3 running old pods: the failure is visible at both the deployment and the pod level.")

    print()
    payload: Dict[str, Any] = {"image": BAD_IMAGE}
    print(f"Our PATCH request payload is set to:")
    print(json.dumps(payload, indent=2))

    print("\n- Press ENTER to send the PATCH request, quickly switch to the UI to track its status.")
    _wait()
    response = requests.patch(
        url  = f"{BASE_URL}/{uid}",
        json = payload,
    )
    print(f"Server Responded With Status Code {response.status_code} (the PATCH itself is valid).")

    print("\n- Press ENTER to continue to the next status.\n")
    _wait()

def crash_loop() -> None:

    _separator()
    print("STATUS: FAILING due to CrashLoopBackOff.")
    print(f"We'll create a deployment using the payload:")
    print(json.dumps(CRASHER_PAYLOAD, indent=2))
    print()
    print("The container keeps crashing right after it starts, because its command exits with code 1.")
    print("What we expect:")
    print("- The container starts, prints 'Starting up...' and 'Fatal: missing config file /etc/app.conf', then exits with code 1.")
    print("- It has no readiness check, so Kubernetes counts it as ready the moment it starts: for a second, the status may show HEALTHY (1/1 ready).")
    print("- Once it has crashed, no replica is ready, so the status changes to FAILING (the document's 'no replicas ready'). Its container state is Error (terminated, exit code 1).")
    print("- Kubernetes restarts the container every time it exits (a Deployment's pods always restart), waiting longer after each crash: 10 s, then 20 s, 40 s and so on. While it waits, the container's state is CrashLoopBackOff, which also makes the status FAILING (the document's 'CrashLoopBackOff detected').")
    print("- In the UI, the deployment turns red, and its pod table shows CrashLoopBackOff with a climbing restart count. In the Logs panel, tick 'Previous container' to read why it crashed.")

    print("\n- Press ENTER to create it, then switch to the UI to track its status.")
    _wait()
    data: Dict[str, Any] = _create(CRASHER_PAYLOAD)

    print("\n- Press ENTER to continue to the next status.\n")
    _wait()

def unschedulable() -> None:

    _separator()
    print("STATUS: FAILING, because its pod cannot be scheduled.")
    print(
        f"A pod that asks for {TOO_BIG_PAYLOAD['resources']['requests']['cpu']} CPUs, more than the node has. "
        "In a namespace the API created, the quota would refuse this with a 403 (Part A). This one goes to "
        f"`{TOO_BIG_PAYLOAD['namespace']}`, which existed before and has no quota, so Kubernetes itself decides: it "
        "accepts the Deployment, but its scheduler cannot place the pod, which stays Pending."
    )
    print("What we expect:")
    print("- The create succeeds (201): Kubernetes accepts the Deployment and creates its pod. So this is not a failed Kubernetes API call (no 500); the problem only shows up in the deployment's status.")
    print(f"- The scheduler finds no node with {TOO_BIG_PAYLOAD['resources']['requests']['cpu']} free CPUs, so the pod stays Pending, with no node and no container, and its PodScheduled condition is False with reason Unschedulable ('Insufficient cpu').")
    print("- An unschedulable pod makes the status FAILING straight away. Kubernetes itself keeps retrying the pod, and keeps reporting the rollout as in progress (Progressing=True) until its 10-minute progress deadline.")
    print("- In the UI, the deployment turns red and its pod shows Pending, with no node. The API's pod view doesn't include the scheduler's message; `kubectl describe pod` shows it under Events.")
    print("\nThe request payload is:")
    print(json.dumps(TOO_BIG_PAYLOAD, indent=2))

    print("\n- Press ENTER to create it, then switch to the UI to track its status.")
    _wait()
    data: Dict[str, Any] = _create(TOO_BIG_PAYLOAD)

    print("\n- Press ENTER to end the demo.\n")
    _wait()

if (__name__ == "__main__"):
    try:

        print()
        print(
            "In this demo, we will see the failure modes associated with our API and within the cluster.\n"
            "I recommend cross-checking the API's response data and what we expect to happen within the cluster "
            "using the UI (`http://127.0.0.1:5000`)"
        )
        print()
        print("NOTE: Please make sure that the API is live (`python run.py` is running).")
        print()
        print("Press ENTER to continue.")
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
        # However the demo ends (finished, Ctrl+C or an error), delete everything it created.
        _delete_namespace()
        _delete_too_big()
