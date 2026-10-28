"""Creator bookmarks: persistent storage and the Bookmarks tab.

Bookmarks are stored as a JSON list inside the application's ``QSettings``
instance under the ``creator_bookmarks`` key, which keeps them per-user and
consistent with how the rest of the application stores preferences.
"""

from __future__ import annotations

import json
from typing import Iterable, List, Optional
from urllib.parse import urlsplit

import qtawesome as qta
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from kemonodownloader.domain_config import get_domains
from kemonodownloader.kd_language import translate

__all__ = ["normalize_creator_url", "BookmarkStore", "BookmarksTab"]


def normalize_creator_url(url: str) -> Optional[str]:
    """Return a canonical creator URL, or ``None`` when *url* is not one.

    Accepts ``https://kemono.cr/{service}/user/{user_id}`` (optionally with a
    ``www.`` prefix, extra query string, or missing scheme) and rejects post
    URLs, unrelated hosts and malformed paths.
    """
    if not isinstance(url, str):
        return None
    candidate = url.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = "https://" + candidate.lstrip("/")

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None

    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    known_domains = get_domains()
    if not any(
        host == domain or host.endswith("." + domain) for domain in known_domains
    ):
        return None

    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) != 3 or segments[1] != "user":
        return None

    service, _, user_id = segments
    return f"https://{host}/{service}/user/{user_id}"


def describe_creator_url(url: str) -> str:
    """Return a short ``service / user_id`` hint for display, or an empty string."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) != 3 or segments[1] != "user":
        return ""
    return f"{segments[0]} / {segments[2]}"


class BookmarkStore:
    """Persist a deduplicated, ordered list of creator URLs."""

    KEY = "creator_bookmarks"

    def __init__(self, settings: Optional[QSettings] = None):
        self._settings = (
            settings
            if settings is not None
            else QSettings("VoxDroid", "KemonoDownloader")
        )

    def all(self) -> List[str]:
        """Return the saved creator URLs in insertion order."""
        raw = self._settings.value(self.KEY, "", type=str)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        return [entry for entry in data if isinstance(entry, str)]

    def _save(self, urls: Iterable[str]) -> None:
        self._settings.setValue(self.KEY, json.dumps(list(urls)))
        self._settings.sync()

    def add(self, url: str) -> bool:
        """Bookmark *url*. Returns ``False`` when invalid or already saved."""
        normalized = normalize_creator_url(url)
        if normalized is None:
            return False
        urls = self.all()
        if normalized in urls:
            return False
        urls.append(normalized)
        self._save(urls)
        return True

    def remove(self, url: str) -> bool:
        """Remove *url*. Returns ``False`` when it was not bookmarked."""
        normalized = normalize_creator_url(url) or url
        urls = self.all()
        if normalized not in urls:
            return False
        urls.remove(normalized)
        self._save(urls)
        return True

    def contains(self, url: str) -> bool:
        normalized = normalize_creator_url(url) or url
        return normalized in self.all()

    def clear(self) -> None:
        self._save([])


class BookmarksTab(QWidget):
    """Tab listing saved creator URLs and pushing them into the creator queue."""

    def __init__(self, parent, store: Optional[BookmarkStore] = None):
        super().__init__()
        self.parent = parent
        self.store = store if store is not None else BookmarkStore()
        self.setup_ui()
        self.refresh_bookmarks()

        settings_tab = getattr(self.parent, "settings_tab", None)
        if settings_tab is not None:
            try:
                settings_tab.language_changed.connect(self.update_ui_text)
            except (AttributeError, TypeError):
                pass

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        self.title_label = QLabel()
        self.title_label.setStyleSheet("color: white; padding: 5px;")
        layout.addWidget(self.title_label)

        self.intro_label = QLabel()
        self.intro_label.setStyleSheet("color: #D0D0D0; padding: 5px;")
        self.intro_label.setWordWrap(True)
        layout.addWidget(self.intro_label)

        input_layout = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setStyleSheet("padding: 5px; border-radius: 5px;")
        self.url_input.returnPressed.connect(self.add_from_input)
        input_layout.addWidget(self.url_input, stretch=1)

        self.add_button = QPushButton(qta.icon("fa5s.plus", color="white"), "")
        self.add_button.setStyleSheet(self._button_style())
        self.add_button.clicked.connect(self.add_from_input)
        input_layout.addWidget(self.add_button)
        layout.addLayout(input_layout)

        actions_layout = QHBoxLayout()
        self.import_button = QPushButton(
            qta.icon("fa5s.file-import", color="white"), ""
        )
        self.import_button.setStyleSheet(self._button_style())
        self.import_button.clicked.connect(self.import_from_file)
        actions_layout.addWidget(self.import_button)

        self.clear_button = QPushButton(qta.icon("fa5s.trash", color="white"), "")
        self.clear_button.setStyleSheet(self._button_style())
        self.clear_button.clicked.connect(self.clear_all)
        actions_layout.addWidget(self.clear_button)
        actions_layout.addStretch()

        self.count_label = QLabel()
        self.count_label.setStyleSheet("color: #D0D0D0;")
        actions_layout.addWidget(self.count_label)
        layout.addLayout(actions_layout)

        self.bookmark_list = QListWidget()
        self.bookmark_list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection
        )
        self.bookmark_list.setStyleSheet(
            "QListWidget { background: #2A3B5A; border-radius: 5px; color: white; }"
        )
        layout.addWidget(self.bookmark_list, stretch=1)

        queue_layout = QHBoxLayout()
        self.add_selected_button = QPushButton()
        self.add_selected_button.setStyleSheet(self._button_style())
        self.add_selected_button.clicked.connect(
            lambda: self.push_to_queue(selected_only=True)
        )
        queue_layout.addWidget(self.add_selected_button)

        self.add_all_button = QPushButton()
        self.add_all_button.setStyleSheet(self._button_style())
        self.add_all_button.clicked.connect(
            lambda: self.push_to_queue(selected_only=False)
        )
        queue_layout.addWidget(self.add_all_button)
        layout.addLayout(queue_layout)

        self.setMinimumSize(300, 400)
        self.update_ui_text()

    @staticmethod
    def _button_style():
        return (
            "QPushButton { background: #4A5B7A; color: white; padding: 6px 12px;"
            " border-radius: 5px; }"
            "QPushButton:hover { background: #5A6B8A; }"
            "QPushButton:disabled { color: gray; }"
        )

    def update_ui_text(self):
        self.title_label.setText(f"<h1>{translate('bookmarks_title')}</h1>")
        self.intro_label.setText(translate("bookmarks_intro"))
        self.url_input.setPlaceholderText(translate("bookmark_url_placeholder"))
        self.add_button.setText(translate("add_bookmark"))
        self.import_button.setText(translate("import_bookmarks_from_file"))
        self.clear_button.setText(translate("clear_all_bookmarks"))
        self.add_selected_button.setText(translate("add_selected_to_queue"))
        self.add_all_button.setText(translate("add_all_bookmarks_to_queue"))
        self.update_count_label()
        self.refresh_bookmarks()

    def refresh_ui(self):
        """Refresh hook called by the main window on settings/language changes."""
        self.refresh_bookmarks()

    def showEvent(self, a0):
        # Pick up bookmarks saved from other tabs (auto-bookmark, star toggle).
        self.refresh_bookmarks()
        super().showEvent(a0)

    def update_count_label(self):
        self.count_label.setText(
            translate("bookmarks_count", len(self.store.all()))
        )

    # ------------------------------------------------------------------
    # Bookmark management
    # ------------------------------------------------------------------

    def refresh_bookmarks(self):
        """Rebuild the bookmark list from the store."""
        self.bookmark_list.clear()
        for url in self.store.all():
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, url)

            widget = QWidget()
            row = QHBoxLayout()
            row.setContentsMargins(5, 2, 5, 2)
            row.setSpacing(5)

            hint = describe_creator_url(url)
            label = QLabel(f"{url}\n<small>{hint}</small>" if hint else url)
            label.setStyleSheet("color: white;")
            row.addWidget(label, stretch=1)

            remove_button = QPushButton(qta.icon("fa5s.times", color="white"), "")
            remove_button.setStyleSheet(
                "background: #4A5B7A; border-radius: 5px;"
                " min-width: 30px; max-width: 30px;"
                " min-height: 30px; max-height: 30px;"
            )
            remove_button.clicked.connect(self.create_remove_handler(url))
            row.addWidget(remove_button)

            widget.setLayout(row)
            item.setSizeHint(widget.sizeHint())
            self.bookmark_list.addItem(item)
            self.bookmark_list.setItemWidget(item, widget)
            setattr(widget, "label", label)
            setattr(widget, "remove_button", remove_button)

        self.update_count_label()

    def create_remove_handler(self, url):
        def handler():
            self.store.remove(url)
            self.refresh_bookmarks()

        return handler

    def add_from_input(self):
        url = self.url_input.text().strip()
        if not url:
            return
        if normalize_creator_url(url) is None:
            QMessageBox.warning(
                self,
                translate("warning"),
                translate("bookmark_invalid_url", url),
            )
        elif self.store.contains(url):
            QMessageBox.information(
                self,
                translate("info"),
                translate("bookmark_already_saved", url),
            )
        else:
            self.store.add(url)
            self.url_input.clear()
            self.log(translate("bookmark_added", url))
        self.refresh_bookmarks()

    def import_from_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            translate("select_bookmarks_file"),
            "",
            "Text Files (*.txt);;All Files (*)",
        )
        if not file_path:
            return

        added = 0
        skipped = 0
        try:
            with open(file_path, "r", encoding="utf-8") as file:
                for line in file:
                    entry = line.strip()
                    if not entry:
                        continue
                    if self.store.add(entry):
                        added += 1
                    else:
                        skipped += 1
        except OSError as error:
            QMessageBox.critical(
                self,
                translate("file_read_error_title"),
                translate("file_read_error", str(error)),
            )
            return

        self.refresh_bookmarks()
        QMessageBox.information(
            self,
            translate("bulk_add_complete"),
            translate("bookmarks_import_summary", added, skipped),
        )

    def clear_all(self):
        if not self.store.all():
            return
        reply = QMessageBox.question(
            self,
            translate("confirm_clear_bookmarks"),
            translate("confirm_clear_bookmarks_message"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.store.clear()
            self.refresh_bookmarks()
            self.log(translate("bookmarks_cleared"))

    # ------------------------------------------------------------------
    # Queue hand-off
    # ------------------------------------------------------------------

    def selected_urls(self) -> List[str]:
        urls = []
        for item in self.bookmark_list.selectedItems():
            url = item.data(Qt.ItemDataRole.UserRole)
            if url:
                urls.append(url)
        return urls

    def push_to_queue(self, selected_only: bool = True):
        urls = self.selected_urls() if selected_only else self.store.all()
        if not urls:
            missing_key = (
                "no_bookmarks_selected" if self.store.all() else "no_bookmarks_saved"
            )
            QMessageBox.information(
                self,
                translate("info"),
                translate(missing_key),
            )
            return

        creator_tab = getattr(self.parent, "creator_tab", None)
        enqueue = getattr(creator_tab, "enqueue_creator_urls", None)
        if enqueue is None:
            return

        try:
            added, skipped = enqueue(urls)
        except (TypeError, ValueError):
            added, skipped = len(urls), 0

        self.log(translate("bookmarks_queue_filled", added, skipped))
        self.switch_to_creator_tab()

    def switch_to_creator_tab(self):
        tabs = getattr(self.parent, "tabs", None)
        creator_tab = getattr(self.parent, "creator_tab", None)
        if tabs is None or creator_tab is None:
            return
        try:
            tabs.setCurrentWidget(creator_tab)
        except (AttributeError, TypeError, RuntimeError):
            pass

    def log(self, message: str):
        logger = getattr(self.parent, "log", None)
        if callable(logger):
            logger(message)
