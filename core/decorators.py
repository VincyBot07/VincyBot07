import warnings

from core.utils import trigger_typing as _trigger_typing


def trigger_typing(func):
    warnings.warn(
        "trigger_typing è stato spostato a core.utils.trigger_typing, questo sarà rimosso.",
        DeprecationWarning,
        stacklevel=2,
    )
    return _trigger_typing(func)
