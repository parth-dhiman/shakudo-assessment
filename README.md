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
