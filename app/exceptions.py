class NotFoundError(Exception):
    def __init__(self, uid: str, namespace: str | None):
        location: str = f' in namespace "{namespace}"' if namespace else ""
        super().__init__(f'Deployment UID "{uid}" not found{location}.')
        self.namespace = namespace
        self.uid      = uid