class RecordNotFoundError(ValueError):
  """A requested record does not exist within the supplied workspace."""


class ArtifactRegistrationError(ValueError):
  """A supplied file or kind cannot be registered as a workspace artifact."""
