"""Shared execution policy types used by isolated E4 action modules."""


class AutomaticExecutionBlocked(RuntimeError):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


class AutomaticPostEffectUncertain(RuntimeError):
    """An external effect may have occurred; retain the running receipt for proof."""
