"""Errors raised by reelpost."""


class ReelpostError(Exception):
    """A problem the user can act on."""


class GraphAPIError(ReelpostError):
    """Meta Graph API returned an error payload or an HTTP failure."""

    def __init__(self, message: str, *, code: int | None = None, subcode: int | None = None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode


class UploadError(ReelpostError):
    """A binary upload to rupload.facebook.com failed."""

    def __init__(self, message: str, *, retriable: bool = False):
        super().__init__(message)
        self.retriable = retriable
