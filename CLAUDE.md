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
Filled at the start of the assessment from TASK.md. Edit only between these markers.
<!-- PROJECT:END -->
