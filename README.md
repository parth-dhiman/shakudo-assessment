# Shakudo Assessment: Kubernetes Workload Deployment & Monitoring Platform

A service that manages and monitors workload deployments on your local Kubernetes cluster: a REST API, a web UI and two demo scripts, all running against a real minikube cluster.

## Available Endpoints

| Method | Endpoint | Description |
| ------ | -------- | ----------- |
| POST   | `/api/deployments` | Create a new Deployment in the cluster. Accepts JSON body with workload spec. Returns the created deployment with its UID and initial status. |
| GET    | `/api/deployments` | List all managed deployments. Support filtering by namespace via query parameter. Return current status from the cluster, not cached data. |
| GET    | `/api/deployments/:id` | Get detailed status for a single deployment including: replica counts (desired, ready, available, unavailable), pod-level status for each pod (name, phase, restart count, container statuses), conditions array from the Deployment status. |
| PATCH  | `/api/deployments/:id` | Update a deployment. Must support at minimum: scaling replica count, updating the container image (triggering a rolling update). |
| POST   | `/api/deployments/:id/restart` | Trigger a rollout restart (equivalent to kubectl rollout restart). This should patch the pod template annotation to force new pods. |
| GET    | `/api/deployments/:id/history` | *(Stretch goal)* Deployment history: track rollout history. |
| POST   | `/api/deployments/:id/rollback` | *(Stretch goal)* Support rollback to a previous revision (using the Kubernetes rollout API). |
| DELETE | `/api/deployments/:id` | Delete the deployment and its associated resources from the cluster. Should handle cascade deletion of owned ReplicaSets and Pods. |
| GET    | `/api/deployments/:id/logs?pod=<name>` | *(Stretch goal)* Log streaming: real-time log tailing from running pods. |

`:id` is the deployment's Kubernetes UID, returned by the POST. Every `:id` endpoint also accepts an optional `?namespace=<namespace>` to narrow the lookup.

## Running the Demo

### Prerequisites

- Python 3.11 or newer
- Git
- [minikube](https://minikube.sigs.k8s.io/docs/start/), started with `minikube start`. The quota in `quota.yaml` is sized for `minikube start --cpus=4 --memory=8192`.
- `kubectl`, pointing at the minikube cluster (`minikube start` sets this up). The API connects through your kubeconfig, and the demo scripts also use `kubectl` to clean up after themselves.

### 1. Set up

Clone the repository and install the dependencies in a virtual environment:

```bash
git clone https://github.com/parth-dhiman/shakudo-assessment
cd shakudo-assessment

python3 -m venv venv               # Windows: python -m venv venv
source venv/bin/activate           # Windows (PowerShell): venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

### 2. Start the API

```bash
python run.py
```

The API and the web UI listen on http://127.0.0.1:5000. Leave this terminal running.

### 3. Run the demo scripts

In a second terminal, go to the `shakudo-assessment` directory again, activate the virtual environment, and run either script:

```bash
# Every endpoint, including the stretch goals.
python -m scripts.demo

# Failure modes: the API's error responses, and how its health status reports deployments that break.
python -m scripts.failure
```

Each script explains every step and waits for you to press ENTER before sending a request. When it ends, it deletes everything it created, even if you stop it with Ctrl+C.

## Using the UI

With `python run.py` running (step 2 above), open http://127.0.0.1:5000/ in your browser:

- The dashboard lists every deployment across namespaces, colour-coded by health (green, yellow, red), with a namespace filter. It refreshes every 5 seconds.
- Click a deployment for its detail view: replica counts, a pod table, the deployment's conditions, and a Logs panel that streams the selected pod's log live.
- From the detail view you can scale, change the image, restart, or delete (with confirmation). Create new deployments with the "New deployment" button.

## Running the Tests

```bash
python -m pytest tests/unit -q          # no cluster needed
python -m pytest tests/integration -q   # needs minikube running
```

## Points I'd Like to Mention

- **`lastUpdated` in the response to a PATCH (or restart) still shows the previous update time.** The API takes a deployment's last update time from the newest `lastUpdateTime` among its conditions, and those conditions are written by Kubernetes' deployment controller a moment *after* a change. The PATCH response is built from the object that `patch_namespaced_deployment` returns straight away, so its conditions, and therefore `lastUpdated`, still describe the state before the change. A GET a few seconds later shows the new time. This could be fixed by taking the time from `metadata.managedFields[].time` (I only learned about this right now), which the API server updates on every write (including our own PATCH), or by waiting for the controller to process the change (`status.observedGeneration` catching up with `metadata.generation`) before responding, as the rollback endpoint already does.

- **Timestamps in the demo video may be varied.** The video was not recorded in one go: it combines parts of several runs, so the pod, container and deployment timestamps in the responses shown can jump between clips.

- **FAILING covers more than CrashLoopBackOff.** The document defines FAILING as "no replicas ready or CrashLoopBackOff detected". The API also reports FAILING when a pod cannot pull its image (`ErrImagePull` / `ImagePullBackOff`), when a pod cannot be scheduled (`Unschedulable`), and when a rollout misses its progress deadline (`ProgressDeadlineExceeded`). These count even while older pods are still serving: during a bad-image rollout the deployment is FAILING with 3/3 ready, whereas Kubernetes itself keeps calling that rollout "progressing" until its 10-minute progress deadline. The full rules, checked in this order (the first match wins):

  | Status | When |
  | ------ | ---- |
  | UNKNOWN | Kubernetes has not processed the latest change yet (`status.observedGeneration` < `metadata.generation`). |
  | FAILING | The rollout missed its progress deadline, or a pod cannot be scheduled, cannot pull its image, or is in CrashLoopBackOff. |
  | PROGRESSING | A rollout is under way: Kubernetes has not marked it complete, not every pod runs the new template yet, or old pods are still around. |
  | FAILING | Replicas are wanted, but none of them is ready. |
  | DEGRADED | Some, but not all, replicas are ready. |
  | HEALTHY | Every replica is ready (also when 0 replicas are wanted). |

- **An unreachable Kubernetes API returns 503, not 500.** The API returns 503 `K8S_UNREACHABLE` when the API server cannot be reached, since the service it depends on is unavailable. Other failed Kubernetes calls return 500 `KUBERNETES_ERROR`, except Kubernetes' own 404, 409 and 422 answers, which become 404, 409 and 400.

- **"Insufficient resources" shows up in two ways.** A request that would exceed a namespace's quota is refused by the API itself with 403 `QUOTA_EXCEEDED`, the same status Kubernetes uses for quota violations. In a namespace without a quota, Kubernetes accepts an oversized deployment (201), but its pod stays Pending because no node has room, and the API reports it as FAILING.

- **Resource quotas only apply to namespaces the API creates.** Each one gets a ResourceQuota on CPU and memory requests from `quota.yaml`; namespaces that already existed, such as `default`, get none. Because of the quota, every create must include `resources.requests.cpu` and `resources.requests.memory`. Two limitations:
  - The quota check runs on create and on scale-up. Image updates, restarts and rollbacks briefly start extra pods (25% more, at least 1) and can stall in a namespace that is nearly full.
  - The check is not atomic: two requests at the same moment can both pass. Kubernetes still refuses whichever pods do not fit.

- **Health checks accept HTTP probes only** (liveness and readiness). A container's `ready` flag in the detail view serves as its probe status.

- **Log streaming uses Server-Sent Events rather than a WebSocket.** Logs only flow from the server to the browser, and SSE needs no extra dependency. Each stream starts with the pod's last 100 lines, then follows new ones.

- **Rolling back renumbers revisions.** As with `kubectl rollout undo`, the restored template becomes the newest revision, so its old number leaves the history. Rolling back to a revision that is not in the history returns 400 `REVISION_NOT_FOUND`.
