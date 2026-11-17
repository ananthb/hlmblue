"""Whether a BLE link to the unit is still worth keeping.

Pure, like power.py, so the decision is testable without Home Assistant.
"""

from __future__ import annotations


def deaf(on: bool, heard_at: float | None, now: float, timeout: float) -> bool:
    """Whether a running unit has gone quiet on a link that still looks up.

    A link can stay open and carry nothing: the proxy holds the connection,
    every write returns, and the unit never pushes a status frame again. Nothing
    else notices, so the entity sat unavailable until the integration was
    reloaded. The coordinator drops such a link and lets the next poll rebuild
    it, which is what brings the unit back.

    Silence is not a fault while the unit is off: it stops pushing within a
    minute of switching off and does not answer a status request at all. Only a
    running unit that has not been heard from for `timeout` counts as broken.
    `heard_at` is the later of the link coming up and the last status frame
    decoded, or None when there is no reference yet.
    """
    if not on or heard_at is None:
        return False
    return now - heard_at >= timeout
