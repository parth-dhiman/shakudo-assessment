# Standard imports.
import json
import subprocess
import time
from typing import (
    Any,
    Dict,
    Optional,
)

# Third-party imports.
import requests

BASE_URL: str = "http://127.0.0.1:5000/api/deployments"

# ==================== Payloads (tweak these) ====================

CREATE_PAYLOAD: Dict[str, Any] = {

    # Required.
    "name":  "web",
    "image": "nginx:latest",
    # Each namespace the API creates has a quota on CPU and memory requests, so both requests are required.
    # Limits are optional, and must not be below the requests.
    "resources": {
        "requests": {"cpu": "50m",  "memory": "64Mi"},
        "limits":   {"cpu": "250m", "memory": "128Mi"},
    },

    # Optional.
    "namespace": "demo",                             # created (with its quota) if missing; default "default"
    "replicas":  3,                                  # default 1
    "port":      80,                                 # the container port nginx listens on
    "command":   ["/docker-entrypoint.sh"],          # overrides the image's ENTRYPOINT; this is nginx's own
    "args":      ["nginx", "-g", "daemon off;"],     # overrides the image's CMD; this is nginx's own too
    "env":       {"GREETING": "hello"},
    "labels":    {"team": "demo"},                   # set on the Deployment and its pods

    # Stretch goal: a Service in front of the pods; its port comes back in the response.
    "service": {
        "type":        "NodePort",
        "port":        80,
        "target_port": 80,
        "protocol":    "TCP",
    },

    # Stretch goal: HTTP health checks. nginx answers "/" with 200, so both pass.
    "readiness_probe": {
        "http_get":              {"port": 80, "path": "/", "scheme": "HTTP"},
        "initial_delay_seconds": 2,
        "period_seconds":        5,
        "timeout_seconds":       2,
        "success_threshold":     1,
        "failure_threshold":     3,
    },
    "liveness_probe": {
        "http_get":              {"port": 80, "path": "/"},
        "initial_delay_seconds": 5,
        "period_seconds":        10,
        "timeout_seconds":       2,
        "failure_threshold":     3,                  # (success_threshold must be 1 for liveness, its default)
    },
}

# Scale 3 -> 5 and change the image: the image change starts a rolling update, i.e. a new revision.
PATCH_PAYLOAD: Dict[str, Any] = {
    "replicas": 5,
    "image":    "nginx:alpine",
}

# Back to revision 1, the template we created (nginx:latest). An empty body would pick the previous revision.
ROLLBACK_PAYLOAD: Dict[str, Any] = {"revision": 1}

# How long the log stream is followed before the demo moves on.
LOG_SECONDS: int = 15

# ==================== Helpers ====================

def _name() -> str:
    return CREATE_PAYLOAD["name"]

def _namespace() -> str:
    return CREATE_PAYLOAD.get("namespace", "default")

def _wait() -> None:
    input("")

def _separator() -> None:
    print("-"*100 + '\n')

def _kubectl(*commands: str) -> None:
    print("\nRun this command to check this on your actual cluster:" if len(commands) == 1
          else "\nRun these commands to check this on your actual cluster:")
    for command in commands:
        print(f"    {command}")

def _delete_namespace() -> None:
    # Demo housekeeping, not part of the API: removes everything this run created, even after Ctrl+C.
    # kubectl waits until the namespace is gone, so a rerun never meets a namespace that is still terminating.
    namespace: str = _namespace()
    if namespace in ("default", "kube-system", "kube-public", "kube-node-lease"):
        return
    command = ["kubectl", "delete", "namespace", namespace, "--ignore-not-found"]
    print(f"\nCleaning up: {' '.join(command)}  (this takes a few seconds)")
    try:
        subprocess.run(command)
    except FileNotFoundError:
        print(f"kubectl not found; delete the namespace yourself: {' '.join(command)}")

# ==================== One function per endpoint ====================

def create_deployment() -> Optional[str]:

    _separator()
    print("Endpoint 1: POST	/api/deployments")
    print(
        "Description (as provided in document): Create a new Deployment in the cluster. " \
        "Accepts JSON body with workload spec (a payload has been pre-defined in `app/scripts/demo.py`). " \
        "Returns the created deployment with its UID and initial status.\n"
    )
    print(
        "The payload also covers two stretch goals: a NodePort Service (its port comes back in the response) "
        "and readiness/liveness health checks. A namespace the API creates also gets a CPU/memory quota.\n"
    )
    print("Our request payload is set to:")
    print(json.dumps(CREATE_PAYLOAD, indent=2, default=str))

    print("\n- Press ENTER to make a request to this endpoint.")
    _wait()

    response = requests.post(
        url  = BASE_URL,
        json = CREATE_PAYLOAD,
    )
    data: Dict[str, Any] = response.json()
    
    print(f"Server Responded With Status Code {response.status_code}")
    print("The Response Body is:")
    print(json.dumps(data, indent=2, default=str))

    _kubectl(f"kubectl get deployment,service,resourcequota -n {_namespace()}")
    if data.get("nodePort"):
        print("\nAnd reach nginx through the Service:")
        print(f"    curl http://$(minikube ip):{data['nodePort']}")

    print("\n- Press ENTER to continue to the next endpoint.\n")
    _wait()

    return data.get("uid")

def list_deployments() -> None:

    payload = CREATE_PAYLOAD.copy()
    payload.update({
        "name": "web-1"
    })
    response = requests.post(
        url  = BASE_URL,
        json = payload,
    )

    _separator()
    print("Endpoint 2: GET /api/deployments")
    print(
        "Description (as provided in document): List all managed deployments. "
        "Support filtering by namespace via query parameter. "
        "Return current status from the cluster, not cached data.\n"
    )
    namespace: str = CREATE_PAYLOAD['namespace']
    print(
        "IMPORTANT NOTE: An additional example deployment has been automatically created "
        f"within the namespace `{namespace}`, which is the same namespace as the previously "
        "created deployment. As a result, this endpoint will return a list of 2 deployments "
        "instead of a single deployment.\n"
        f"Also, the `namespace` request parameter is optional, but it has been set to "
        f"`{namespace}` for this request."
)

    print("\n- Press ENTER to make a request to this endpoint.")
    _wait()

    response = requests.get(
        BASE_URL,
        params = {
            "namespace": namespace,
        }
    )
    data: Dict[str, Any] = response.json()

    print(f"Server Responded With Status Code {response.status_code}")
    print("The Response Body is:")
    print(json.dumps(data, indent=2, default=str))

    _kubectl(f"kubectl get deployments -n {namespace}")

    print("\n- Press ENTER to continue to the next endpoint.\n")
    _wait()

def get_deployment(uid: str) -> None:

    _separator()
    print("Endpoint 3: GET /api/deployments/<uid>")
    print(
        "Get detailed status for a single deployment including: "
        "replica counts (desired, ready, available, unavailable), "
        "pod-level status for each pod (name, phase, restart count, container statuses), "
        "conditions array from the Deployment status."
    )

    print("\n- Press ENTER to make a request to this endpoint (for the deployment we first created).")
    _wait()

    response = requests.get(f"{BASE_URL}/{uid}")
    data: Dict[str, Any] = response.json()

    print(f"Server Responded With Status Code {response.status_code}")
    print("The Response Body is:")
    print(json.dumps(data, indent=2, default=str))

    _kubectl(f"kubectl get deployment,replicaset,pod -n {_namespace()} -l app.kubernetes.io/name={_name()}")

    print("\n- Press ENTER to continue to the next endpoint.\n")
    _wait()

def patch_deployment(uid: str) -> None:

    _separator()
    print("Endpoint 4: PATCH /api/deployments/<uid>")
    print(
        "Update a deployment. Must support at minimum: "
        "scaling replica count, updating the container image "
        "(triggering a rolling update)."
    )

    print("Our request payload is set to:")
    print(json.dumps(PATCH_PAYLOAD, indent=2, default=str))


    print("\n- Press ENTER to make a request to this endpoint.")
    _wait()

    response = requests.patch(
        url = f"{BASE_URL}/{uid}",
        json = PATCH_PAYLOAD,
    )
    data: Dict[str, Any] = response.json()

    print(f"Server Responded With Status Code {response.status_code}")
    print("The Response Body is:")
    print(json.dumps(data, indent=2, default=str))

    _kubectl(f"kubectl get deployment {_name()} -n {_namespace()} -o wide")

    print("\n- Press ENTER to continue to the next endpoint.\n")
    _wait()

def restart_deployment(uid: str) -> None:

    _separator()
    print("Endpoint 5: POST /api/deployments/<uid>/restart")
    print(
        "Trigger a rollout restart (equivalent to kubectl rollout restart)."
        "This should patch the pod template annotation to force new pods."
    )

    print("\n- Press ENTER to make a request to this endpoint.")
    _wait()

    response = requests.post(f"{BASE_URL}/{uid}/restart")
    data: Dict[str, Any] = response.json()

    print(f"Server Responded With Status Code {response.status_code}")
    print("The Response Body is:")
    print(json.dumps(data, indent=2, default=str))

    _kubectl(f"kubectl get deployment {_name()} -n {_namespace()} -o jsonpath='{{.spec.template.metadata.annotations}}'")

    print("\n- Press ENTER to continue to the next endpoint.\n")
    _wait()

def rollback_info(uid: str) -> None:

    _separator()
    print("Endpoint 6: GET /api/deployments/<uid>/history")
    print(
        "Deployment history (stretch goal): track rollout history. "
        "Kubernetes keeps one ReplicaSet per pod template it has rolled out, numbered by revision; "
        "this lists them newest first and marks the current one.\n"
    )

    print("\n- Press ENTER to make a request to this endpoint.")
    _wait()

    response = requests.get(f"{BASE_URL}/{uid}/history")
    data: Dict[str, Any] = response.json()

    print(f"Server Responded With Status Code {response.status_code}")
    print("The Response Body is:")
    print(json.dumps(data, indent=2, default=str))

    _kubectl(f"kubectl rollout history deployment/{_name()} -n {_namespace()}")

    print("\n- Press ENTER to continue to the next endpoint.\n")
    _wait()

def rollback_deployment(uid: str) -> None:

    _separator()
    print("Endpoint 7: POST /api/deployments/<uid>/rollback")
    print(
        "Deployment rollback (stretch goal): roll back to a previous revision, as `kubectl rollout undo` does. "
        "An empty body rolls back to the previous revision; this payload picks revision 1, the nginx:latest template.\n"
        "Kubernetes re-numbers the restored template as the newest revision (4), so revision 1 leaves the history."
    )
    print("\nOur request payload is set to:")
    print(json.dumps(ROLLBACK_PAYLOAD, indent=2))

    print("\n- Press ENTER to make a request to this endpoint.")
    _wait()

    response = requests.post(
        url  = f"{BASE_URL}/{uid}/rollback",
        json = ROLLBACK_PAYLOAD,
    )
    data: Dict[str, Any] = response.json()

    print(f"Server Responded With Status Code {response.status_code}")
    print("The Response Body is:")
    print(json.dumps(data, indent=2, default=str))

    _kubectl(
        f"kubectl get deployment {_name()} -n {_namespace()} -o wide",
    )

    print("\n- Press ENTER to continue to the next endpoint.\n")
    _wait()

def delete_deployment(uid: str) -> None:

    _separator()
    print("Endpoint 8: DELETE /api/deployments/<uid>")
    print(
        "Delete the deployment and its associated resources from the cluster. "
        "Should handle cascade deletion of owned ReplicaSets and Pods."
    )

    print("\n- Press ENTER to make a request to this endpoint.")
    _wait()

    response = requests.delete(f"{BASE_URL}/{uid}")

    print(f"Server Responded With Status Code {response.status_code}")

    # The Service goes too: it names the Deployment as its owner.
    _kubectl(f"kubectl get deployment,replicaset,pod,service -n {_namespace()} -l app.kubernetes.io/name={_name()}    (empty after a few seconds)")

    print("\n- Press ENTER to end the demo.\n")
    _wait()

def stream_logs(uid: str) -> None:

    _separator()
    print("Endpoint 9: GET /api/deployments/<uid>/logs?pod=<pod>")
    print(
        "Log streaming (stretch goal): real-time log tailing from a running pod. "
        "The response is a Server-Sent Events stream: the pod's last 100 log lines, then each new line as it is written "
        "(the web page's Logs panel shows the same stream).\n"
        "nginx logs every request, including the readiness and liveness checks, so new lines keep arriving."
    )

    # Prefer a pod whose container is running; right after the rollback, the new pods may still be starting.
    pods = requests.get(f"{BASE_URL}/{uid}").json().get("pods", [])
    running = [pod["name"] for pod in pods if any("running" in (c.get("state") or {}) for c in pod["containers"])]
    names = sorted(running or [pod["name"] for pod in pods])
    if not names:
        print("\nThis deployment has no pods right now, so there is no log to show.")
        print("\n- Press ENTER to continue to the next endpoint.\n")
        _wait()
        return
    pod: str = names[0]
    print(f"\nThe pod we will follow is `{pod}`, for {LOG_SECONDS} seconds.")

    print("\n- Press ENTER to make a request to this endpoint.")
    _wait()

    response = requests.get(
        f"{BASE_URL}/{uid}/logs",
        params  = {"pod": pod},
        stream  = True,
        timeout = (5, LOG_SECONDS + 20),
    )
    print(f"Server Responded With Status Code {response.status_code} ({response.headers.get('Content-Type')})")
    print("Log lines, as they arrive:")

    # Each event is an optional "event: <name>" line, a "data: <text>" line and a blank line.
    # Plain events are log lines; "unavailable" means the container has no log yet; "end" closes the stream.
    deadline: float = time.monotonic() + LOG_SECONDS
    event: Optional[str] = None
    try:
        for line in response.iter_lines(decode_unicode=True):
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                if (event == "end"):
                    break
                text: str = line[len("data: "):]
                print(f"    (no log yet: {text})" if (event == "unavailable") else f"    | {text}")
                event = None
            if (time.monotonic() > deadline):
                break
    finally:
        response.close()

    _kubectl(f"kubectl logs {pod} -n {_namespace()} --tail=100 -f")

    print("\n- Press ENTER to continue to the next endpoint.\n")
    _wait()

if (__name__ == "__main__"):
    try:

        print()
        print(
            "In this demo, we will send requests to all the API endpoints within this project.\n"
            "I recommend cross-checking the API's response data and what we expect to happen within the cluster\n"
            "either using the UI (`http://127.0.0.1:5000/`) or using `kubectl` commands we print along the way."
        )
        print()
        print("NOTE: Please make sure that:\n" 
            "- Minikube is running.\n"
            "- The API is live (`python run.py` is running)."
        )
        print()
        print("Press ENTER to continue.")
        _wait()

        uid: str = create_deployment()
        if (uid is None):
            raise SystemExit("Create failed; fix CREATE_PAYLOAD and run again.")
        list_deployments()
        get_deployment(uid)
        patch_deployment(uid)
        restart_deployment(uid)
        rollback_info(uid)
        rollback_deployment(uid)
        delete_deployment(uid)

    except requests.ConnectionError:
        raise SystemExit("Cannot reach the API. Start it first: venv/bin/python run.py")
    except KeyboardInterrupt:
        print("\nDemo interrupted.")
    finally:
        # However the demo ends (finished, Ctrl+C or an error), delete the namespace it worked in.
        _delete_namespace()
