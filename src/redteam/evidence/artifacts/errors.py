"""Errors raised by Track B's internal Artifact storage."""


class ArtifactError(Exception):
    """Base class. Error text never contains Artifact contents."""


class ArtifactReferenceError(ArtifactError, ValueError):
    """The supplied reference is not an opaque reference issued by this backend."""


class ArtifactNotFoundError(ArtifactError, FileNotFoundError):
    """A valid reference has no stored Artifact."""


class ArtifactIntegrityError(ArtifactError):
    """Stored state is unsafe to read, for example because it is a symlink."""


class ArtifactTooLargeError(ArtifactError, ValueError):
    """Artifact exceeds the backend's configured size limit."""


class ArtifactWriteError(ArtifactError, OSError):
    """Artifact could not be completely and atomically published."""
