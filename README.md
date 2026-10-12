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
