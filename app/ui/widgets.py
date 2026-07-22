"""可复用 UI 组件：下载进度对话框等。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)


def human_size(n: int) -> str:
    if not n:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.1f} {units[i]}"


class DownloadProgressDialog(QDialog):
    """下载进度对话框，支持取消。"""

    canceled = Signal()

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(420)

        self._label = QLabel("准备下载…")
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._stat = QLabel("")

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self._on_cancel)

        layout = QVBoxLayout(self)
        layout.addWidget(self._label)
        layout.addWidget(self._bar)
        layout.addWidget(self._stat)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(cancel_btn)
        layout.addLayout(row)

        self._canceled = False

    def _on_cancel(self) -> None:
        self._canceled = True
        self.canceled.emit()

    def update_progress(self, downloaded: int, total: int) -> None:
        if total > 0:
            pct = int(downloaded * 100 / total)
            self._bar.setValue(pct)
            self._stat.setText(f"{human_size(downloaded)} / {human_size(total)}")
        else:
            self._bar.setValue(0)
            self._stat.setText(human_size(downloaded))

    def set_status(self, text: str) -> None:
        self._label.setText(text)


def show_info(parent, title: str, text: str) -> None:
    QMessageBox.information(parent, title, text)


def show_error(parent, title: str, text: str) -> None:
    QMessageBox.critical(parent, title, text)


def ask_yes(parent, title: str, text: str) -> bool:
    return QMessageBox.question(parent, title, text, QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes
