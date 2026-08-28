"""Project-specific exceptions."""


class ConfigurationError(ValueError):
    """Raised when an engagement configuration cannot be loaded."""


class PolicyViolation(RuntimeError):
    """Base class for denied or over-budget actions."""


class ScopeViolation(PolicyViolation):
    """Raised when a request falls outside the approved scope."""


class RequestLimitExceeded(PolicyViolation):
    """Raised when the total request budget is exhausted."""


class RateLimitExceeded(PolicyViolation):
    """Raised when the configured request rate is exceeded."""


class RuntimeLimitExceeded(PolicyViolation):
    """Raised when the engagement runtime budget is exhausted."""


class DnsPolicyViolation(PolicyViolation):
    """Raised when a target resolves to a forbidden or changed address."""


class PlanPolicyViolation(PolicyViolation):
    """Raised when a planner proposes an unsupported or out-of-scope test."""


class CapabilityPolicyViolation(PolicyViolation):
    """Raised when a proposed agent capability is not explicitly authorized."""


class ProbeRegistrationError(RuntimeError):
    """Raised when a capability probe is missing or registered more than once."""


class ToolAdapterError(RuntimeError):
    """Base class for fail-closed tool-adapter failures."""


class ToolTransportUnavailable(ToolAdapterError):
    """Raised when an external tool transport is not configured or reachable."""


class ToolProtocolError(ToolAdapterError):
    """Raised when a tool request or response violates the adapter contract."""


class QueueTransitionError(RuntimeError):
    """Raised when a queued action attempts an invalid state transition."""
