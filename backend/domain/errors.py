"""Typed domain failures exposed consistently by API and workers."""


class DomainError(Exception):
    """Base class for expected business failures."""

    code = "domain_error"


class ValidationFailure(DomainError):
    code = "validation"


class PolicyFailure(DomainError):
    code = "policy"


class DependencyFailure(DomainError):
    code = "dependency"


class ResourceNotFound(DomainError):
    code = "not_found"


class InvalidTransition(DomainError):
    code = "invalid_transition"


class CitationValidationFailure(ValidationFailure):
    code = "invalid_citation"
