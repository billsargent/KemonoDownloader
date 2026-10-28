import gc
import os
import sys
from pathlib import Path

import pytest

# Add the src directory to the python path
src_path = Path(__file__).parent.parent / "src"
sys.path.append(str(src_path))

# Hold a strong reference so the QApplication is not garbage-collected.
_qapp_instance = None


def pytest_configure(config):
    """Create a QApplication early so modules that import Qt at import-time can succeed.

    This runs before collection, preventing ImportError during test collection on headless CI.
    The offscreen platform is set via pyproject.toml [tool.pytest.ini_options]
    qt_qpa_platform = "offscreen", but we also apply it here as a safety net.
    """
    global _qapp_instance
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        import faulthandler

        from PyQt6.QtWidgets import QApplication

        # Enable faulthandler to get native tracebacks on crashes (useful for C-level faults)
        try:
            faulthandler.enable()
        except Exception:
            pass

        if QApplication.instance() is None:
            _qapp_instance = QApplication([])
    except Exception:
        # If PyQt is not available, allow tests to fail later rather than crash here.
        pass


# NOTE: No custom ``qapp`` fixture here.  pytest-qt provides a session-scoped
# ``qapp`` fixture that correctly manages the QApplication lifecycle.  Defining
# a function-scoped override previously shadowed it and could return None when
# the early QApplication created above was garbage-collected.


@pytest.fixture(autouse=True)
def block_external_open_calls(monkeypatch):
    """Prevent the test suite from launching real browser windows.

    ``KemonoDownloader.show_update_notification`` opens the default browser once
    its dialog is accepted, and Qt rich-text labels can open external links.
    Tests run headless, so both paths are neutralised and the attempted URLs are
    recorded on the fixture value.
    """
    import webbrowser

    opened = []

    def _record(url, *args, **kwargs):
        opened.append(str(url))
        return True

    monkeypatch.setattr(webbrowser, "open", _record)
    monkeypatch.setattr(webbrowser, "open_new", _record)
    monkeypatch.setattr(webbrowser, "open_new_tab", _record)
    try:
        from PyQt6.QtGui import QDesktopServices

        monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(_record))
    except Exception:
        pass
    # Windows only: opening a directory launches File Explorer.
    import os as _os

    if hasattr(_os, "startfile"):
        monkeypatch.setattr(_os, "startfile", _record, raising=False)

    return opened


@pytest.fixture()
def isolated_settings(tmp_path):
    """Provide a QSettings instance backed by an isolated temp INI file.

    This prevents tests from reading/writing the real application settings.
    """
    from PyQt6.QtCore import QSettings

    ini_path = str(tmp_path / "test_settings.ini")
    settings = QSettings(ini_path, QSettings.Format.IniFormat)
    settings.clear()
    yield settings
    settings.clear()


@pytest.fixture()
def isolated_hash_dir(tmp_path):
    """Return a temp directory suitable for HashDB tests."""
    return str(tmp_path / "hash_store")


@pytest.fixture(autouse=True)
def cleanup_qt_threads_after_test():
    """Best-effort cleanup for leaked QThreads.

    Some tests intentionally exercise cancellation/error paths and may leave
    thread objects alive briefly. On Linux CI, a surviving running QThread can
    trigger a native abort ("QThread: Destroyed while thread is still running").
    This fixture aggressively stops and joins any running QThread wrappers
    after each test to keep the process stable.
    """
    yield

    try:
        from PyQt6.QtCore import QCoreApplication, QThread
    except Exception:
        return

    app = QCoreApplication.instance()
    if app is not None:
        try:
            app.processEvents()
        except Exception:
            pass

    for obj in list(gc.get_objects()):
        try:
            if not isinstance(obj, QThread):
                continue
            if not obj.isRunning():
                continue

            # Cooperatively request shutdown first.
            try:
                if hasattr(obj, "stop"):
                    obj.stop()
            except Exception:
                pass
            try:
                obj.requestInterruption()
            except Exception:
                pass
            try:
                obj.quit()
            except Exception:
                pass
            try:
                obj.wait(2000)
            except Exception:
                pass

            # Last resort: terminate stubborn workers.
            try:
                if obj.isRunning():
                    obj.terminate()
                    obj.wait(1000)
            except Exception:
                pass
        except Exception:
            # Ignore invalid/deleted sip wrappers during GC traversal.
            pass

    if app is not None:
        try:
            app.processEvents()
        except Exception:
            pass
