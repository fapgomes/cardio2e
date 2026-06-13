"""pytest fixtures and import setup for the cardio2e test suite."""

import os
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)

# Make both the repo root (for ``cardio2e_modules``) and the tests dir
# (for ``_fakes``) importable regardless of how pytest is invoked.
for _path in (_REPO_ROOT, _TESTS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from _fakes import FakeSerial, RecordingMqttClient, install_paho_stub  # noqa: E402

# Install a paho 2.x-like stub so importing cardio2e_mqtt works without the
# real dependency. Individual tests may reinstall a 1.x variant and reload.
install_paho_stub(with_callback_api_version=True)


@pytest.fixture(autouse=True)
def _no_serial_throttle(monkeypatch):
    """Disable the 150ms inter-command throttle so tests don't sleep."""
    import cardio2e_modules.cardio2e_serial as cs

    monkeypatch.setattr(cs, "_MIN_COMMAND_INTERVAL", 0, raising=False)


@pytest.fixture
def mqtt():
    return RecordingMqttClient()


@pytest.fixture
def serial_conn():
    return FakeSerial()


@pytest.fixture
def app_state():
    from cardio2e_modules.cardio2e_config import AppState

    return AppState()


@pytest.fixture
def sample_config_path():
    return os.path.join(_REPO_ROOT, "cardio2e.conf-sample")
