class GrooveSerpentError(Exception):
    """Base exception for user-facing Groove Serpent failures."""


class DependencyError(GrooveSerpentError):
    """Raised when FFmpeg or another required executable is unavailable."""


class ProjectValidationError(GrooveSerpentError):
    """Raised when a project file is malformed or internally inconsistent."""


class UnsupportedCaptureError(GrooveSerpentError):
    """Raised when a source falls outside the declared capture envelope."""


class ExportError(GrooveSerpentError):
    """Raised when an audio export fails."""
