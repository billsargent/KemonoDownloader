"""Tests for creator bookmarks: URL normalisation, storage, and tab behaviour."""

from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QFileDialog, QMessageBox

from kemonodownloader.creator_downloader import CreatorDownloaderTab
from kemonodownloader.kd_bookmarks import (
    BookmarksTab,
    BookmarkStore,
    describe_creator_url,
    normalize_creator_url,
)

CREATOR_URL = "https://kemono.cr/patreon/user/12345"
OTHER_CREATOR_URL = "https://coomer.st/onlyfans/user/abc123"


# ----------------------------------------------------------------------
# URL normalisation
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (CREATOR_URL, CREATOR_URL),
        (CREATOR_URL + "/", CREATOR_URL),
        ("  " + CREATOR_URL + "  ", CREATOR_URL),
        ("kemono.cr/patreon/user/12345", CREATOR_URL),
        ("https://kemono.cr/patreon/user/12345?page=2", CREATOR_URL),
        ("https://www.kemono.cr/patreon/user/12345", CREATOR_URL),
    ],
)
def test_normalize_creator_url_accepts_variants(raw, expected):
    assert normalize_creator_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        None,
        "https://kemono.cr/patreon/user/12345/post/98765",
        "https://example.com/patreon/user/12345",
        "https://kemono.cr/patreon/12345",
        "https://kemono.cr/user/12345",
        "ftp://kemono.cr/patreon/user/12345",
    ],
)
def test_normalize_creator_url_rejects_invalid(raw):
    assert normalize_creator_url(raw) is None


def test_describe_creator_url():
    assert describe_creator_url(CREATOR_URL) == "patreon / 12345"
    assert describe_creator_url("https://example.com/nope") == ""


# ----------------------------------------------------------------------
# Store
# ----------------------------------------------------------------------


def test_store_add_contains_remove(isolated_settings):
    store = BookmarkStore(isolated_settings)

    assert store.all() == []
    assert store.add(CREATOR_URL) is True
    assert store.add(CREATOR_URL) is False  # duplicate
    assert store.add(CREATOR_URL + "/") is False  # duplicate after normalisation
    assert store.add("https://example.com/a/user/b") is False  # invalid

    assert store.all() == [CREATOR_URL]
    assert store.contains(CREATOR_URL) is True

    assert store.remove(CREATOR_URL) is True
    assert store.remove(CREATOR_URL) is False
    assert store.all() == []


def test_store_persists_across_instances(isolated_settings):
    first = BookmarkStore(isolated_settings)
    first.add(CREATOR_URL)
    first.add(OTHER_CREATOR_URL)

    second = BookmarkStore(isolated_settings)
    assert second.all() == [CREATOR_URL, OTHER_CREATOR_URL]


def test_store_clear_and_corrupt_payload(isolated_settings):
    store = BookmarkStore(isolated_settings)
    store.add(CREATOR_URL)
    store.clear()
    assert store.all() == []

    isolated_settings.setValue(BookmarkStore.KEY, "not json")
    assert store.all() == []

    isolated_settings.setValue(BookmarkStore.KEY, '{"a": 1}')
    assert store.all() == []


# ----------------------------------------------------------------------
# Creator tab integration
# ----------------------------------------------------------------------


class _FakeTextInput:
    def __init__(self, text=""):
        self._text = text

    def toPlainText(self):
        return self._text

    def clear(self):
        self._text = ""


class _FakeLineInput:
    def __init__(self, text=""):
        self._text = text

    def text(self):
        return self._text

    def clear(self):
        self._text = ""


class _FakeWidget:
    def setRange(self, *args):
        pass

    def setValue(self, *args):
        pass

    def setText(self, *args):
        pass


class _FakeBookmarksTab:
    def __init__(self, store):
        self.store = store
        self.refreshes = 0

    def refresh_bookmarks(self):
        self.refreshes += 1


def _bare_creator_tab(bookmarks_tab=None):
    """Create a CreatorDownloaderTab without building its Qt widget tree."""
    tab = CreatorDownloaderTab.__new__(CreatorDownloaderTab)
    tab.creator_queue = []
    tab._parent = SimpleNamespace()
    if bookmarks_tab is not None:
        tab._parent.bookmarks_tab = bookmarks_tab
    tab.logs = []
    tab.update_creator_queue_list = lambda: None
    tab.append_log_to_console = lambda message, level="INFO": tab.logs.append(
        (message, level)
    )
    return tab


def test_enqueue_creator_urls_counts_and_dedupes():
    tab = _bare_creator_tab()

    added, skipped = tab.enqueue_creator_urls(
        [CREATOR_URL, CREATOR_URL + "/", "https://kemono.cr/post/1"]
    )

    assert (added, skipped) == (1, 2)
    assert [url for url, _ in tab.creator_queue] == [CREATOR_URL]

    added, skipped = tab.enqueue_creator_urls([CREATOR_URL])
    assert (added, skipped) == (0, 1)


def test_enqueue_creator_urls_bookmarks_added_creators(isolated_settings):
    store = BookmarkStore(isolated_settings)
    tab = _bare_creator_tab(_FakeBookmarksTab(store))

    tab.enqueue_creator_urls([CREATOR_URL])

    assert store.all() == [CREATOR_URL]


def test_remember_creator_without_bookmarks_tab_is_noop():
    tab = _bare_creator_tab()
    tab._remember_creator(CREATOR_URL)  # must not raise


def test_add_multiple_creators_bookmarks_each_url(isolated_settings):
    store = BookmarkStore(isolated_settings)
    tab = _bare_creator_tab(_FakeBookmarksTab(store))
    tab.creator_multi_url_input = _FakeTextInput(
        "{}\n{}\nnot-a-url".format(CREATOR_URL, OTHER_CREATOR_URL)
    )

    tab.add_multiple_creators_to_queue()

    assert store.all() == [CREATOR_URL, OTHER_CREATOR_URL]


def test_on_validation_finished_bookmarks_url(isolated_settings):
    store = BookmarkStore(isolated_settings)
    tab = _bare_creator_tab(_FakeBookmarksTab(store))
    tab.background_task_progress = _FakeWidget()
    tab.background_task_label = _FakeWidget()
    tab.creator_url_input = _FakeLineInput(CREATOR_URL)

    tab.on_validation_finished(CREATOR_URL, True)

    assert store.all() == [CREATOR_URL]


def test_add_creators_from_file_bookmarks_urls(
    isolated_settings, tmp_path, monkeypatch
):
    store = BookmarkStore(isolated_settings)
    tab = _bare_creator_tab(_FakeBookmarksTab(store))

    file_path = tmp_path / "creators.txt"
    file_path.write_text("{}\n{}\n".format(CREATOR_URL, OTHER_CREATOR_URL), "utf-8")

    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *a, **k: (str(file_path), "")),
    )
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))

    tab.add_creators_from_file()

    assert store.all() == [CREATOR_URL, OTHER_CREATOR_URL]


def test_bookmark_handler_toggles(isolated_settings):
    store = BookmarkStore(isolated_settings)
    bookmarks_tab = _FakeBookmarksTab(store)
    tab = _bare_creator_tab(bookmarks_tab)

    handler = tab.create_bookmark_handler(CREATOR_URL)

    handler()
    assert store.all() == [CREATOR_URL]
    assert bookmarks_tab.refreshes == 1

    handler()
    assert store.all() == []
    assert bookmarks_tab.refreshes == 2


# ----------------------------------------------------------------------
# Bookmarks tab
# ----------------------------------------------------------------------


def _make_tab(qapp, isolated_settings):
    store = BookmarkStore(isolated_settings)
    captured = {}

    class FakeCreatorTab:
        def enqueue_creator_urls(self, urls):
            captured["urls"] = list(urls)
            return len(urls), 0

    class FakeTabs:
        def __init__(self):
            self.current = None

        def setCurrentWidget(self, widget):
            self.current = widget

    fake_tabs = FakeTabs()
    creator_tab = FakeCreatorTab()
    parent = SimpleNamespace(
        settings_tab=None,
        creator_tab=creator_tab,
        tabs=fake_tabs,
        log=lambda message: captured.setdefault("log", message),
    )
    tab = BookmarksTab(parent, store=store)
    return tab, store, captured, fake_tabs, creator_tab


def test_bookmarks_tab_add_and_render(qapp, isolated_settings):
    tab, store, captured, _, _ = _make_tab(qapp, isolated_settings)

    tab.url_input.setText(CREATOR_URL)
    tab.add_from_input()

    assert store.all() == [CREATOR_URL]
    assert tab.bookmark_list.count() == 1
    assert tab.url_input.text() == ""
    assert "1 bookmark" in tab.count_label.text()


def test_bookmarks_tab_rejects_invalid_url(qapp, isolated_settings, monkeypatch):
    tab, store, _, _, _ = _make_tab(qapp, isolated_settings)
    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *a, **k: warnings.append(a)),
    )

    tab.url_input.setText("https://example.com/not/a/creator")
    tab.add_from_input()

    assert store.all() == []
    assert warnings


def test_bookmarks_tab_push_selected_to_queue(qapp, isolated_settings, monkeypatch):
    tab, store, captured, fake_tabs, creator_tab = _make_tab(qapp, isolated_settings)
    store.add(CREATOR_URL)
    store.add(OTHER_CREATOR_URL)
    tab.refresh_bookmarks()

    tab.bookmark_list.item(0).setSelected(True)
    tab.push_to_queue(selected_only=True)

    assert captured["urls"] == [CREATOR_URL]
    assert fake_tabs.current is creator_tab
    assert "1" in captured["log"]


def test_bookmarks_tab_push_all_without_bookmarks_warns(
    qapp, isolated_settings, monkeypatch
):
    tab, _, captured, fake_tabs, _ = _make_tab(qapp, isolated_settings)
    infos = []
    monkeypatch.setattr(
        QMessageBox, "information", staticmethod(lambda *a, **k: infos.append(a))
    )

    tab.push_to_queue(selected_only=False)

    assert infos
    assert "urls" not in captured
    assert fake_tabs.current is None


def test_bookmarks_tab_remove_and_clear(qapp, isolated_settings, monkeypatch):
    tab, store, _, _, _ = _make_tab(qapp, isolated_settings)
    store.add(CREATOR_URL)
    tab.refresh_bookmarks()

    tab.create_remove_handler(CREATOR_URL)()
    assert store.all() == []
    assert tab.bookmark_list.count() == 0

    store.add(CREATOR_URL)
    tab.refresh_bookmarks()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    tab.clear_all()

    assert store.all() == []
    assert tab.bookmark_list.count() == 0


def test_bookmarks_tab_import_from_file(qapp, isolated_settings, tmp_path, monkeypatch):
    tab, store, _, _, _ = _make_tab(qapp, isolated_settings)
    file_path = tmp_path / "bookmarks.txt"
    file_path.write_text(
        "{}\n\ninvalid\n{}\n".format(CREATOR_URL, OTHER_CREATOR_URL), "utf-8"
    )

    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *a, **k: (str(file_path), "")),
    )
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))

    tab.import_from_file()

    assert store.all() == [CREATOR_URL, OTHER_CREATOR_URL]
    assert tab.bookmark_list.count() == 2


def test_bookmarks_tab_update_ui_text(qapp, isolated_settings):
    from kemonodownloader.kd_language import translate

    tab, _, _, _, _ = _make_tab(qapp, isolated_settings)
    tab.update_ui_text()

    assert tab.add_button.text() == translate("add_bookmark")
    assert tab.add_selected_button.text() == translate("add_selected_to_queue")
    assert translate("bookmarks_tab") in tab.title_label.text()


def test_bookmarks_tab_placement_in_main_window(qapp, monkeypatch):
    import kemonodownloader.app as app_module

    class StubTab(__import__("PyQt6.QtWidgets", fromlist=["QWidget"]).QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.settings = {
                "base_directory": "/tmp",
                "base_folder_name": "Kemono",
                "language": "english",
            }
            self.language_changed = SimpleNamespace(connect=lambda *a, **k: None)
            self.font_changed = SimpleNamespace(connect=lambda *a, **k: None)
            self.download_started = SimpleNamespace(connect=lambda *a, **k: None)
            self.download_finished = SimpleNamespace(connect=lambda *a, **k: None)

        def get_font(self):
            return "Arial"

        def is_auto_check_updates_enabled(self):
            return False

        def refresh_ui(self):
            pass

        def update_ui_text(self):
            pass

    for name in (
        "SettingsTab",
        "PostDownloaderTab",
        "CreatorDownloaderTab",
        "ThumbnailDownloaderTab",
        "HelpTab",
        "ExtensionTab",
    ):
        monkeypatch.setattr(app_module, name, StubTab)

    window = app_module.KemonoDownloader()
    try:
        main_widget = window.setup_main_ui()
        window.main_widget = main_widget
        assert window.tabs.widget(1) is window.creator_tab
        assert window.tabs.widget(2) is window.bookmarks_tab
        assert window.tabs.widget(3) is window.thumbnail_tab
        assert window.tabs.count() == 7
    finally:
        # The intro screen keeps a reference to the window; drop it first.
        window.intro_screen = None
        window.deleteLater()
