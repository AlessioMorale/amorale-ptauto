"""Exception hierarchy for ptauto.

Everything the CLI catches and renders as a clean message derives from
`PtAutoError`; anything else is a bug and is allowed to traceback.
"""

from __future__ import annotations


class PtAutoError(Exception):
    """Base class for every error ptauto reports to the user."""


class SpecError(PtAutoError):
    """The YAML network specification is invalid or inconsistent."""


class BridgeError(PtAutoError):
    """Packet Tracer is not reachable over any channel."""


class PTError(PtAutoError):
    """Packet Tracer accepted the command but reported a failure."""


class TimeoutError_(PtAutoError):
    """Packet Tracer did not answer in time."""


# `TimeoutError` shadows the builtin, so the class is defined with a trailing
# underscore and exported under the readable name for `except ptauto.PTTimeout`.
PTTimeout = TimeoutError_
