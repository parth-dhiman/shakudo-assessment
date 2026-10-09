# Standard imports.
import json
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
    "name":      "web",
    "namespace": "demo",
    "image":     "nginx:latest",
    "replicas":  3,
    # Optional extras:
    #   "port": 80,
    #   "env": {"GREETING": "hi"},
    #   "labels": {"team": "demo"},
    #   "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"cpu": "500m", "memory": "128Mi"}},
    # Errors to try: "replicas": -1 (400), "name": "Web_App" (400), "image": "NGINX" (400).
}

SCALE_PAYLOAD: Dict[str, Any] = {"replicas": 5}

# A tag that does not exist: health turns FAILING and the new pod shows ErrImagePull / ImagePullBackOff.
IMAGE_PAYLOAD: Dict[str, Any] = {"image": "nginx:nonexistent"}

# ==================== Helpers ====================

def _name() -> str:
    return CREATE_PAYLOAD["name"]

def _namespace() -> str:
    return CREATE_PAYLOAD.get("namespace", "default")

def _show(response: requests.Response, verify: str) -> None:
    print(f"{response.request.method} {response.url} -> {response.status_code}")
    if response.content:
        print(json.dumps(response.json(), indent=2))
    print(f"\nVerify with:  {verify}\n")

def _pause() -> None:
    input("Press Enter for the next step...\n")

# ==================== One function per endpoint ====================

def create_deployment() -> Optional[str]:
    response = requests.post(BASE_URL, json=CREATE_PAYLOAD)
    _show(response, f"kubectl get deployment {_name()} -n {_namespace()}")

    # 409: it already exists from an earlier run, so reuse it.
    if (response.status_code == 409):
        items = requests.get(BASE_URL, params={"namespace": _namespace()}).json()["items"]
        return next((item["uid"] for item in items if item["name"] == _name()), None)
    return response.json().get("uid")

def list_deployments() -> None:
    response = requests.get(BASE_URL)
    _show(response, "kubectl get deployments -A -l app.kubernetes.io/managed-by")

def list_deployments_in_namespace() -> None:
    response = requests.get(BASE_URL, params={"namespace": _namespace()})
    _show(response, f"kubectl get deployments -n {_namespace()}")

def get_deployment(uid: str) -> None:
    response = requests.get(f"{BASE_URL}/{uid}")
    _show(response, f"kubectl get deployment,replicaset,pod -n {_namespace()} -l app.kubernetes.io/name={_name()}")

def scale_deployment(uid: str) -> None:
    response = requests.patch(f"{BASE_URL}/{uid}", json=SCALE_PAYLOAD)
    _show(response, f"kubectl get deployment {_name()} -n {_namespace()}    (READY heads to x/{SCALE_PAYLOAD['replicas']})")

def restart_deployment(uid: str) -> None:
    response = requests.post(f"{BASE_URL}/{uid}/restart")
    _show(response, f"kubectl rollout history deployment/{_name()} -n {_namespace()}    (one more revision)")

def update_image(uid: str) -> None:
    response = requests.patch(f"{BASE_URL}/{uid}", json=IMAGE_PAYLOAD)
    _show(response, f"kubectl get deployment {_name()} -n {_namespace()} -o wide && kubectl get pods -n {_namespace()}")

def delete_deployment(uid: str) -> None:
    response = requests.delete(f"{BASE_URL}/{uid}")
    _show(response, f"kubectl get deployment,replicaset,pod -n {_namespace()} -l app.kubernetes.io/name={_name()}    (empty after a few seconds)")

# ==================== Run them in order ====================

if __name__ == "__main__":
    try:
        uid = create_deployment()
        if (uid is None):
            raise SystemExit("Create failed; fix CREATE_PAYLOAD and run again.")
        _pause()

        list_deployments();               _pause()
        list_deployments_in_namespace();  _pause()
        get_deployment(uid);              _pause()
        scale_deployment(uid);            _pause()
        restart_deployment(uid);          _pause()
        update_image(uid);                _pause()
        get_deployment(uid);              _pause()   # health should now be FAILING
        delete_deployment(uid)
    except requests.ConnectionError:
        raise SystemExit("Cannot reach the API. Start it first: venv/bin/python run.py")
