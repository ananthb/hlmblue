"""A running unit that has not been heard from within the timeout means the
link only looks up; silence while off is normal, and no reference yet says
nothing.

link.py is pure and imports nothing of its siblings, but load it the same way
as the other tests so no test needs the integration's __init__, which needs
Home Assistant itself.
"""

import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_DIR = ROOT / "custom_components" / "hlmblue"


def _load():
    package = types.ModuleType("hlmblue_under_test")
    package.__path__ = [str(MODULE_DIR)]
    sys.modules[package.__name__] = package
    full = f"{package.__name__}.link"
    spec = importlib.util.spec_from_file_location(full, MODULE_DIR / "link.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


link = _load()

TIMEOUT = 90.0


def test_off_and_silent_is_not_deaf():
    """An idle unit stops pushing and never answers; that silence is normal."""
    assert not link.deaf(False, heard_at=0.0, now=1000.0, timeout=TIMEOUT)


def test_on_and_recently_heard_is_not_deaf():
    """Ten seconds into a ninety-second window is a healthy link."""
    assert not link.deaf(True, heard_at=100.0, now=110.0, timeout=TIMEOUT)


def test_on_and_quiet_past_the_timeout_is_deaf():
    """A running unit unheard for two minutes is the stuck link from the
    incident: up, writable, and carrying nothing."""
    assert link.deaf(True, heard_at=100.0, now=220.0, timeout=TIMEOUT)


def test_on_with_no_reference_yet_is_not_deaf():
    """Nothing heard and no clock to compare against says nothing yet."""
    assert not link.deaf(True, heard_at=None, now=1000.0, timeout=TIMEOUT)


def test_exactly_at_the_timeout_is_deaf():
    """The boundary counts: quiet for the whole timeout is long enough."""
    assert link.deaf(True, heard_at=100.0, now=100.0 + TIMEOUT, timeout=TIMEOUT)
