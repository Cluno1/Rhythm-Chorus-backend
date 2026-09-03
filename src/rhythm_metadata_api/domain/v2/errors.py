class V2DomainError(Exception):
    status_code = 422
    problem_type = "domain-validation"
    title = "Domain validation failed"

    def __init__(self, detail: str, **extensions: object) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extensions = extensions


class V2NotFound(V2DomainError):
    status_code = 404
    problem_type = "not-found"
    title = "Resource not found"


class V2Conflict(V2DomainError):
    status_code = 409
    problem_type = "domain-conflict"
    title = "Domain conflict"


class StaleRevision(V2DomainError):
    status_code = 412
    problem_type = "stale-revision"
    title = "Resource revision is stale"

    def __init__(self, expected: str, current: str) -> None:
        super().__init__(
            f"Expected {expected}, current revision is {current}",
            current_etag=current,
        )


class IdempotencyConflict(V2Conflict):
    problem_type = "idempotency-key-reused"
    title = "Idempotency key was reused"
