# CLAUDE.md

@TASK.md

## Context
- Timed (3 h) technical assessment. TASK.md (imported above) is the problem statement and the source of truth.
- I drive in small, explicit steps and need to see every decision. Speed matters; Kubernetes correctness is what's graded.
- Everything runs in WSL (Debian) against a local minikube cluster.

## How to work
- Do exactly the step asked. No neighbouring requirements, unrelated refactors or extras. If you notice something needed elsewhere, say so in one line at the end.
- Read the files you'll change before changing them. Keep diffs small and match the surrounding style.
- After every change run `venv/bin/python -m pytest tests/unit -q` plus any test you added or touched, and report the result line. Never say something works without running it.
- Finish each step with at most 5 lines: files changed, what you ran and the result, decisions made.
- If the last commit is older than 25 minutes (`git log -1 --format=%cr`) and unit tests pass, remind me to /checkpoint.
- Commit and push only through /checkpoint or when I ask. Never rewrite history: no amend, rebase, squash, reset --hard or force-push.
- Call `venv/bin/python` and `venv/bin/pip` directly; shell state doesn't persist between commands. A new package goes into requirements.txt, then `venv/bin/pip install -r requirements.txt`.
- The API listens on http://127.0.0.1:5000 (`HOST`/`PORT` in `app/config.py`). You may start it in the background (`venv/bin/python run.py`) and curl it at exactly that address to check endpoints. Restart it after changing server code.

## Kubernetes correctness
Anything whose correctness depends on Kubernetes semantics — field meanings and defaults, status fields and conditions, controller behaviour, ownerReferences and annotations, API status codes and `reason` values, name/quantity/label validation rules, what a kubectl command does — is verified before you write it, in this order of preference:
1. The live cluster: `kubectl explain <type>.<field>` (version-accurate), `kubectl get ... -o json`, `kubectl apply --dry-run=server -f -` (real validation and defaulting).
2. kubernetes.io docs and API reference (References below).
3. Upstream source when the question is "what exactly does kubectl / the controller do".

Then:
- Cite what you relied on in your reply (URL or command + one-line paraphrase). Add a one-line comment with the link in code only where the logic is non-obvious.
- Docs beat memory. If the docs are silent or ambiguous, say so instead of guessing.
- Stay proportionate: one or two lookups per step. Don't look up Flask, pydantic, argparse or JS.

## When to ask vs decide
Ask in one message — numbered, each with your recommendation and a one-line reason — only when:
- TASK.md leaves the choice to us ("your call", "decide", "your choice", "document what you return") and it isn't DECIDED in "Decisions owed" below;
- TASK.md is ambiguous and the choice changes observable behaviour (status/error codes, health values, required fields or defaults, response shapes the CLI and web page depend on);
- official docs contradict TASK.md;
- the step would delete or change cluster state outside a test namespace or the scenario namespace.

Otherwise decide, use the conventional option, and add one line to README → "Assumptions & decisions". Never ask about naming, formatting, file placement or already-chosen libraries.
When I answer, mark the item DECIDED in "Decisions owed" and record it in the README.

## Architecture
- Layers: `app/api/` (HTTP in/out, thin) → `app/schema/` (request validation, pydantic v2, `extra="forbid"`) → `app/k8s/` (every Kubernetes call) and `app/analysis/` (pure logic over fetched objects).
- `app/api/__init__.py` creates the Flask `app`, registers the error handlers and serves the web page. Route modules, one per object type (e.g. `app/api/deployments.py`), do `from app.api import app` and are imported at the bottom of `app/api/__init__.py`. A new object type means a new module plus one import line there.
- `app/k8s/api.py` builds the API clients (`init_core_api`, `init_apps_api`, `init_batch_api`). `app/k8s/<object>.py` holds every Kubernetes call for that object; no other app code calls the Kubernetes API. Every call passes `_request_timeout` (one constant in `app/config.py`).
- Functions in `app/k8s/` return plain dicts in Kubernetes JSON form (camelCase, the same shape as `kubectl get -o json`), converted with `ApiClient.sanitize_for_serialization`. Everything above them works on those dicts, so recorded kubectl snapshots and live objects go through the same code.
- The installed client (kubernetes 37) generates pydantic models: constructors validate required fields (e.g. `V1Deployment(...)` fails without `spec`), unlike older client versions. Check the model in `venv/lib/python3.13/site-packages/kubernetes/client/models/` rather than relying on memory.
- `app/analysis/` never talks to the cluster: objects in, answer out (e.g. health, rollout status, revision history, ownership filtering). No `app.k8s` import, no I/O, no hidden clock (take `now` as a parameter). This is what makes it testable from recorded snapshots.
- Ownership is by UID through `metadata.ownerReferences` with `controller: true` (e.g. Deployment → ReplicaSet → Pod). Label selectors may narrow a list call; name prefixes are never used.
- Errors: raise an exception and let `app/api/error_handlers.py` turn it into the JSON error response; routes never build error bodies themselves. Kubernetes `ApiException`, urllib3 connection errors and pydantic `ValidationError` are already handled there. Define a new exception in `app/exceptions.py` only when an existing one doesn't fit. No stack traces in responses.
- No caching of cluster reads.
- `cli/` (run as `venv/bin/python -m cli`) and `web/` are HTTP clients of the API only: they never import `kubernetes` or `app`. Anything they display that the API defines (labels, colours, defaults) comes from the API, never a copy.
- The web page is `web/index.html`, served at `/`. Every other file in `web/` is served under `/web/`, so the page loads `/web/app.js`, `/web/style.css`, etc.

## Tests
- `tests/unit/`: no cluster, under 10 s, run after every change.
- `tests/integration/`: needs minikube.
- Fixtures in `tests/conftest.py`: `client` (Flask test client for `app`) and `namespace` (a unique `test-xxxxxx` name, deleted after the test even when it fails; the test or the API creates it). Every test that touches the cluster uses `namespace`.
- Browser (Playwright) and CLI tests need a real HTTP server, not the test client: add a fixture that runs `app` in a background thread when the first such test is written.
- Recorded kubectl snapshots live in `tests/fixtures/`, created with `kubectl get <kinds> -n <ns> -o json > tests/fixtures/<name>.json`. Never hand-write or edit them.
- Table-driven tests use `pytest.mark.parametrize` with readable `ids`.

## Commands
```bash
venv/bin/python run.py                           # API + web page on http://127.0.0.1:5000
venv/bin/python -m pytest tests/unit -q          # fast, no cluster
venv/bin/python -m pytest tests/integration -q   # needs minikube
venv/bin/python -m cli --help                    # CLI; talks to the API only
```

## References
Concepts
- Deployments (other workload types are siblings): https://kubernetes.io/docs/concepts/workloads/controllers/deployment/
- Pod lifecycle — phases, container states, restarts, CrashLoopBackOff: https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/
- Probes: https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/
- Owners and dependents: https://kubernetes.io/docs/concepts/overview/working-with-objects/owners-dependents/
- Garbage collection, propagation policies: https://kubernetes.io/docs/concepts/architecture/garbage-collection/
- Object names, DNS rules: https://kubernetes.io/docs/concepts/overview/working-with-objects/names/
- Recommended labels: https://kubernetes.io/docs/concepts/overview/working-with-objects/common-labels/
- Requests and limits: https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/

API
- Deployment v1 (pod-v1, replica-set-v1 alongside): https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/deployment-v1/
- Quantity: https://kubernetes.io/docs/reference/kubernetes-api/common-definitions/quantity/
- resourceVersion, conflicts, subresources: https://kubernetes.io/docs/reference/using-api/api-concepts/
- Conditions, Status reasons: https://github.com/kubernetes/community/blob/master/contributors/devel/sig-architecture/api-conventions.md

Source — what kubectl / the controller actually does
- rollout_status.go, rollback.go, history.go, objectrestarter.go: https://github.com/kubernetes/kubectl/tree/master/pkg/polymorphichelpers
- Revision annotation, condition helpers: https://github.com/kubernetes/kubernetes/blob/master/pkg/controller/deployment/util/deployment_util.go
- Python client API docs: https://github.com/kubernetes-client/python/tree/master/kubernetes/docs

<!-- PROJECT:START -->
## 1. Goal
Build a REST API that creates, monitors and manages Kubernetes **Deployments** on the local minikube cluster (real cluster, no mocks), with computed health.
Primary objects: Deployment, owned ReplicaSets and Pods (read via ownerReferences); Namespace (created on demand).
Clients: one UI (web page or CLI, see D6), both HTTP-only clients of the API.

## 2. Hard rules (from TASK.md, beyond the fixed sections above)
- [ ] "a working system deployed against real Kubernetes, not a mockup with fake data"; live demo. Unit tests use only recorded kubectl snapshots.
- [ ] Kubernetes access through a client library (never a kubectl subprocess).
- [ ] POST "Returns the created deployment with its UID and initial status"; create the namespace "if it doesn't exist".
- [ ] GET list: namespace filter via query parameter (`?namespace=`); "current status from the cluster, not cached data".
- [ ] GET detail: replicas desired/ready/available/unavailable, per pod name/phase/restart count/container states with reason/node, conditions, creation + last update timestamp.
- [ ] Health enum exactly HEALTHY, DEGRADED, FAILING, PROGRESSING, UNKNOWN. FAILING = "no replicas ready or CrashLoopBackOff detected".
- [ ] `labels` apply to "the deployment and pod template". `command`/`args` optional overrides.
- [ ] Restart "should patch the pod template annotation to force new pods". PATCH supports scale + image (rolling update).
- [ ] DELETE cascades to owned ReplicaSets and Pods.
- [ ] Errors are structured JSON: 400 (bad image, negative replicas, invalid resource values, DNS-incompatible name, missing required field), 404 unknown ID, 409 duplicate name in namespace, **500** "with a meaningful message" when a Kubernetes call fails ("insufficient resources, API server unreachable"). `app/api/error_handlers.py` returns 503 for unreachable: TASK says 500, change it.
- [ ] Scenario to pass: nginx:latest x3, then nginx:nonexistent: ImagePullBackOff visible at deployment AND pod level.
- [ ] UI: green/yellow/red health, detail view (counts, pod table, conditions), scale input, restart, delete with confirmation, poll every 5-10 s, namespace selector (web); CLI equivalents if CLI.
- [ ] Tests: validation, lifecycle (create, list, scale, delete, gone), status-mapping unit, one error case (409).
- [ ] README: how to run, assumptions, "what you'd do differently with more time". Keep a running list of where AI output was wrong (review topic).
- [ ] Many small commits, no squash. Deliver private repo or zip. Scope: Parts 1-3 before any stretch.

## 3. Requirement map (IDs are ours; TASK.md has none)
| ID | Behaviour | Files |
|---|---|---|
| A1 | POST /api/deployments: validate, ensure namespace, create, return uid + status | api/deployments, schema/deployments, k8s/deployments |
| A2 | GET list across namespaces, managed-by label selector, `?namespace=`, health per item | api, k8s, analysis/health; tests/integration |
| A3 | GET /:id detail: counts, pods, conditions, health | api, k8s, analysis/ownership+pods+health |
| A4 | PATCH replicas and/or image | api, schema (patch model), k8s |
| A5 | DELETE with cascade | api, k8s |
| A6 | POST /:id/restart (template annotation) | api, k8s |
| A7 | Request schema and defaults (pydantic, extra=forbid) | schema/deployments, schema/common; tests/unit/test_input.py |
| A8 | Error mapping and bodies | api/error_handlers, exceptions |
| M1-M2 | Deployment-level and pod-level status extraction | analysis/pods, analysis/ownership; tests/unit/test_pods.py |
| M3 | Aggregated health enum | analysis/health, analysis/rollout; tests/unit/test_health.py |
| U1-U5 | Dashboard, detail, actions, polling, namespace filter | web/index.html, app.js, style.css (or cli/) |
| T1-T4 | Validation, lifecycle, status mapping, errors | tests/unit/test_input.py, test_health.py; tests/integration/test_lifecycle.py, test_errors.py, test_bad_image.py |
| S1-S5 | Stretch: logs, quota, history/rollback, probes, Service | cut unless Parts 1-4 are done |

Scaffold renames (placeholders, not done): `app/api/obj.py`, `app/schema/obj.py`, `app/k8s/services/obj.py` become `app/api/deployments.py`, `app/schema/deployments.py`, `app/k8s/deployments.py` (drop `services/` per Architecture); `/api/obj` becomes `/api/deployments`. The scaffold create is a spike (hardcoded nginx, replicas 3, no `_request_timeout`, `MANAGED_BY_VALUE = "test-run"`).

## 4. Decisions owed (ask before implementing; all PENDING)
- D1 DECIDED: UID; `?namespace=` is an optional filter (wrong namespace -> 404). `:id` meaning. Options: UID / `namespace/name` / name + `?namespace=`. Rec: UID. CLI `status/scale/restart/delete <id>` take no namespace, names repeat across namespaces, POST returns the UID; resolve by listing managed Deployments and matching `metadata.uid` (field selectors don't cover uid), no cache.
- D2 PENDING Required/optional + defaults + `env` shape. Rec: required `name`, `image`; defaults namespace=default, replicas=1, port none, no resources, `env` = `[{"name","value"}]` (the EnvVar shape). Reasoning: no requests means BestEffort QoS; limits alone make requests default to limits; requests must be <= limits (server 422).
- D3 DECIDED (rec, with "0 ready -> FAILING" checked after PROGRESSING so new deployments show PROGRESSING; any live owned pod counts, not only current-revision). Health precedence and edge cases. Rec: UNKNOWN (status not observed yet) > FAILING > PROGRESSING > DEGRADED > HEALTHY. FAILING also when a current-revision pod waits in ImagePullBackOff/ErrImagePull/CrashLoopBackOff or Progressing=False/ProgressDeadlineExceeded, even if old pods still serve (the required scenario). Replicas=0 -> HEALTHY (Available is True with 0 desired). Unschedulable Pending pods -> FAILING. Reasoning: the rollout controller never shows image pull errors in conditions until the 600 s deadline.
- D4 PENDING PATCH body. Options: replicas+image only / also env, resources, labels. Rec: `replicas`, `image` only, at least one, extra fields 400. Selector is immutable and a template change is what triggers a rollout.
- D5 propagation DECIDED: Background. Response code still PENDING. DELETE propagation + response. Options: Background / Foreground / Orphan; 204 / 200+body. Rec: Background + 204 (object disappears at once, GC removes RS and Pods asynchronously; Foreground keeps the Deployment visible until dependents are gone).
- D6 DECIDED: web, in `web/` (index.html, app.js, style.css); no CLI, no log streaming. UI: web or CLI. Rec: web (colour coding and polling are explicit requirements, best live demo); CLI only if time remains.
- D7 PENDING Labels and selector. Rec: system labels `app.kubernetes.io/name=<name>` + managed-by on Deployment and pod template; selector = name label only; user labels may not override reserved keys (400). Reasoning: `spec.selector` is immutable and must match template labels; a user label in it would break PATCH/relabel.
- D8 PENDING Response shapes and "last update". Rec: list item = identity + counts + health + timestamps (no pods); detail adds pods and conditions; POST returns detail-shaped body (health likely PROGRESSING/UNKNOWN); `lastUpdated` = max(conditions[].lastUpdateTime), falling back to creationTimestamp (scale does not change it; document).

## 5. Kubernetes semantics to verify
- A3/M2 ownership: controller ownerReferences Deployment -> ReplicaSet -> Pod, old ReplicaSets included during rollout. `kubectl get rs,pods -o json`; owners-dependents doc.
- M1 counts: `kubectl explain deployment.status`; omitted fields (e.g. unavailableReplicas) mean 0.
- M3 rollout complete / in progress / failed: rollout_status.go and deployment_util.go (generation vs observedGeneration, updatedReplicas, ProgressDeadlineExceeded). `kubectl explain deployment.spec.progressDeadlineSeconds`.
- M3 ImagePullBackOff and CrashLoopBackOff: live pods, `status.containerStatuses[].state.waiting.reason` vs `lastState.terminated`; pod-lifecycle doc. Phase stays Pending/Running while waiting.
- M3 scheduling: `kubectl explain pod.status.conditions` (PodScheduled False, Unschedulable).
- M3 replicas=0: Available condition value, via live cluster.
- D8: `kubectl explain deployment.status.conditions.lastUpdateTime`.
- A1 validation: Deployment name is a DNS subdomain but pod hostnames need a label (<=63); verify with `kubectl apply --dry-run=server`. Image strings are not validated by the API server (InvalidImageName only at runtime). Quantity grammar, negatives, requests > limits: quantity doc + dry-run.
- A1 namespace: create-if-missing race; Terminating namespace returns 403 on create; `kubectl explain namespace.status.phase`.
- A8 status mapping: Status `reason` values AlreadyExists (409), NotFound (404), Invalid (422), Forbidden for quota (403); api-conventions.
- A5 cascade: `kubectl explain deleteoptions.propagationPolicy`; garbage-collection doc; confirm RS and Pods vanish after Background delete.
- A6 restart: objectrestarter.go (annotation `kubectl.kubernetes.io/restartedAt` under `spec.template.metadata.annotations`, patch type).
- A4 scale/image: patch content type in python client; scale changes no revision, image change does (`deployment.kubernetes.io/revision`).
- D1 id lookup: `kubectl get deploy -A --field-selector metadata.uid=x` (expected unsupported).

## 6. Build order (each step ends with unit tests green and /checkpoint)
Before step 1: ask all PENDING decisions in one message.
1. Rename scaffold; full A7 schema; validation matrix test T1; 503 -> 500 fix (~20 min).
2. k8s build + create: labels, selector, resources, env, command, namespace, `_request_timeout` constant; POST returns uid+status; integration 409 test (T4).
3. Read path: managed-label list, `?namespace=`, id resolution, 404, GET list/detail raw.
4. analysis: ownership + pod summaries; record fixtures (healthy, bad image, crashloop, mid-rollout) with kubectl; tests.
5. analysis: rollout + health enum; table-driven T3 on fixtures; wire into GET list/detail (demo core).
6. PATCH (replicas, image), restart, DELETE; unit tests for patch bodies.
7. Integration lifecycle T2 and bad-image scenario test against minikube.
8. Web: dashboard, colour coding, namespace filter, 5 s polling.
9. Web: detail view, scale/restart/delete-confirm actions.
10. README (assumptions, decisions, what's cut, AI-corrections list), demo rehearsal on live cluster, full test run. Stretch only after this.
<!-- PROJECT:END -->
