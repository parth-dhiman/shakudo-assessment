## Demo script

Walks through every endpoint, one step at a time.

```bash
venv/bin/python run.py              # terminal 1: start the API
venv/bin/python -m scripts.demo     # terminal 2: press Enter between steps
```

Each step prints the API response and a `kubectl` command to check the cluster agrees.

| Step | API shows | kubectl shows |
|---|---|---|
| Create `web` (3 × `nginx:latest`) in `demo` | 201, `"status": "UNKNOWN"` | `kubectl get deployment web -n demo`: 3/3 after a few seconds |
| List, then list `demo` | `web` in `items` | `kubectl get deployments -n demo` |
| Get | `"status": "HEALTHY"`, 3 pods | `kubectl get deployment,replicaset,pod -n demo -l app.kubernetes.io/name=web` |
| Scale to 5 | `"desired": 5` | `kubectl get deployment web -n demo`: 5/5 |
| Restart | 201 | `kubectl rollout history deployment/web -n demo`: one more revision |
| Image → `nginx:nonexistent` | 200 | `kubectl get pods -n demo`: new pods in `ErrImagePull` / `ImagePullBackOff`, 4 old pods still `Running` |
| Get again | `"status": "FAILING"` | `kubectl get deployment web -n demo`: 4/5 |
| Delete | 204 | the same `get deployment,replicaset,pod` command: nothing left |

Right after a create, PATCH or restart, the response says `UNKNOWN`, because Kubernetes hasn't caught up yet. Get again a second later.

To try other values, edit the payloads at the top of `scripts/demo.py`. To clean up: `kubectl delete namespace demo`.

## Web UI

Start the API with `venv/bin/python run.py` and open http://127.0.0.1:5000. The page (`web/index.html`, `web/app.js`, `web/style.css`; plain HTML, CSS and JS with no build step) talks only to the API.

- **Dashboard**: every managed deployment across namespaces with its health, ready/desired and available counts, age and last update, plus a namespace filter (`#/?namespace=<ns>`).
- **Detail** (click a row, `#/d/<uid>`): replica counts, a pod table (phase, readiness, restarts, node, per-container state with waiting reasons such as `ImagePullBackOff`) and the Deployment's conditions.
- **Actions**: scale, update the image, restart, delete (confirmed by typing the name), and a "New deployment" form.
- The current view refreshes every 5 s without a page reload, pauses while the tab is hidden and shows "Connection lost — retrying" when the API is unreachable.

## Assumptions & decisions

- `:id` is the Deployment's `metadata.uid`, which is unique across the cluster, so `?namespace=` is optional on the `:id` routes and only narrows the search (a UID in another namespace gives 404). Field selectors don't support `metadata.uid`, so lookup lists the managed Deployments and matches the UID.
- `replicas` and `port` must be JSON integers (no booleans or numeric strings); `replicas` is capped at 2147483647 because `spec.replicas` is an int32.
- Resource quantities must match the documented Quantity grammar (ASCII digits; suffixes `Ki…Ei`, `m k M G T P E`, or an exponent) and be non-negative. This is stricter than the API server, which also trims whitespace and accepts `n`/`u`; those get a 400 rather than reaching the cluster.
- DELETE uses `propagationPolicy: Background`, the Kubernetes default: the Deployment disappears at once and the garbage collector removes its ReplicaSets and Pods. A UID precondition makes sure the resolved object is the one deleted.
- Health is decided in this order, first match wins:
  1. UNKNOWN: the controller has not yet observed the latest spec (`observedGeneration < generation`).
  2. FAILING: `ProgressDeadlineExceeded`, an unschedulable pod, or a container waiting in CrashLoopBackOff, ImagePullBackOff or ErrImagePull. A bad image rollout is FAILING even while old pods still serve.
  3. PROGRESSING: the rollout is not complete (Progressing reason is not `NewReplicaSetAvailable`, or old or not-yet-updated pods remain).
  4. FAILING: no replicas ready.
  5. DEGRADED: some but not all replicas ready.
  6. HEALTHY: all replicas ready, including 0 desired.
- Web UI health colours: HEALTHY green, PROGRESSING and DEGRADED amber, FAILING red, UNKNOWN grey (the controller hasn't reported yet, so it is neither good nor bad). Status is always a dot plus the word, never colour alone.
- The web UI's namespace selector lists the namespaces seen in unfiltered deployment lists during the session (plus the selected one), not every namespace in the cluster: the API has no namespace endpoint, and only namespaces with managed deployments are worth filtering on.
- Deleting from the web UI asks you to type the deployment's name; the Delete button stays disabled until it matches exactly.
- Beyond TASK.md's scale, restart and delete, the web UI also has an image-update control and a small create form (name, namespace, image, replicas), so the bad-image scenario can be demoed end to end from the browser.
