class NotFoundError(Exception):
    def __init__(self, uid: str, namespace: str | None):
        location: str = f' in namespace "{namespace}"' if namespace else ""
        super().__init__(f'Deployment UID "{uid}" not found{location}.')
        self.namespace = namespace
        self.uid      = uid

class PodNotFoundError(NotFoundError):
    # A NotFoundError, so the same handler answers 404.
    def __init__(self, pod: str, uid: str):
        Exception.__init__(self, f'Pod "{pod}" not found in deployment UID "{uid}".')
        self.namespace = None
        self.uid       = uid
        self.pod       = pod

class QuotaExceededError(Exception):
    # violations: one per quota, worded like Kubernetes' own ("exceeded quota: <name>, requested: …, used: …, limited: …").
    def __init__(self, namespace: str, violations: list[str]):
        super().__init__(f'Not enough quota in namespace "{namespace}": {"; ".join(violations)}.')
        self.namespace  = namespace
        self.violations = violations

class RevisionNotFoundError(Exception):
    def __init__(self, name: str, revision: int | None):
        if (revision is None):
            message: str = f'Deployment "{name}" has no previous revision to roll back to.'
        else:
            message: str = f'Revision {revision} not found in the rollout history of deployment "{name}".'
        super().__init__(message)
        self.name     = name
        self.revision = revision