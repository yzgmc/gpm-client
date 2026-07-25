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
        self._label.setObjectName("subtitle")
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setFixedHeight(18)
        self._stat = QLabel("")
        self._stat.setObjectName("hint")

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self._on_cancel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self._label)
        layout.addWidget(self._bar)
        layout.addWidget(self._stat)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch()
        row.addWidget(cancel_btn)
        layout.addLayout(row)

        self._canceled = False

    def _on_cancel(self) -> None:
        self._canceled = True
        self.canceled.emit()
        # 立即关闭对话框，不等下载线程退出（下载线程会因 cancel_event 尽快终止）
        self.reject()

    def update_progress(self, downloaded: int, total: int) -> None:
        if total > 0:
            pct = int(downloaded * 100 / total)
            self._bar.setValue(pct)
            self._stat.setText(f"{human_size(downloaded)} / {human_size(total)}")
        else:
            # 服务端未返回 content-length：用不确定模式（滚动条）显示在下载
            self._bar.setRange(0, 0)  # 0~0 = busy indicator 滚动动画
            self._stat.setText(human_size(downloaded))

    def set_status(self, text: str) -> None:
        self._label.setText(text)


def show_info(parent, title: str, text: str) -> None:
    QMessageBox.information(parent, title, text)


def show_error(parent, title: str, text: str) -> None:
    QMessageBox.critical(parent, title, text)


def ask_yes(parent, title: str, text: str) -> bool:
    return QMessageBox.question(parent, title, text, QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes


class LoaderInstallDialog(QDialog):
    """模组加载器安装多阶段进度对话框。

    阶段：下载安装器 → 安装加载器 → 完成。
    由工作线程通过 on_progress/on_done/on_failed 驱动（经 Qt 信号投递到主线程后调用）。

    stages 参数允许自定义阶段（供 Java 自动下载复用，如
    [("download","下载 Java"),("extract","解压 Java"),("done","完成")]），
    不传则用默认的加载器安装阶段。stage 的 key 与 progress 回调的 stage 一一对应。
    """

    canceled = Signal()

    # 默认阶段（加载器安装）：key 与 loader_installer 的 progress stage 对应
    _DEFAULT_STAGES = [("download", "下载安装器"), ("install", "安装加载器"), ("done", "完成")]

    def __init__(self, title: str, loader_name: str, parent=None, stages=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(480)

        self._stages = list(stages) if stages else list(self._DEFAULT_STAGES)
        self._stage_labels: dict[str, QLabel] = {}
        self._stage_bars: dict[str, QProgressBar] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        title_lbl = QLabel(f"正在安装 {loader_name}")
        title_lbl.setObjectName("subtitle")
        layout.addWidget(title_lbl)

        # 每个阶段一行：状态点 + 阶段名 + 进度条
        for key, name in self._stages:
            row = QHBoxLayout()
            row.setSpacing(8)
            dot = QLabel("○")
            dot.setStyleSheet("font-size: 16px; color: #6B7280;")
            dot.setFixedWidth(20)
            lbl = QLabel(name)
            row.addWidget(dot)
            row.addWidget(lbl, 1)
            layout.addLayout(row)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setFixedHeight(14)
            layout.addWidget(bar)
            self._stage_labels[key] = dot
            self._stage_bars[key] = bar

        self._detail = QLabel("准备中…")
        self._detail.setWordWrap(True)
        self._detail.setObjectName("hint")
        layout.addWidget(self._detail)

        self._log = QLabel("")
        self._log.setWordWrap(True)
        self._log.setStyleSheet(
            "font-size: 11px; color: #6B7280; background:#121317; "
            "padding:8px; border:1px solid #2A2E39; border-radius:6px; max-height:120px;"
        )
        layout.addWidget(self._log)

        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self._on_cancel)
        self._done_btn = QPushButton("完成")
        self._done_btn.setObjectName("primary")
        self._done_btn.setEnabled(False)
        self._done_btn.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch()
        row.addWidget(self._cancel_btn)
        row.addWidget(self._done_btn)
        layout.addLayout(row)

        self._canceled = False
        self._log_lines: list[str] = []

    def _on_cancel(self) -> None:
        self._canceled = True
        self.canceled.emit()

    # ---------- 由工作线程经信号投递到主线程后调用 ----------
    def on_progress(self, stage: str, detail: str, pct: int) -> None:
        # 当前阶段进行中（● 主题色琥珀金），已过阶段完成（● 成功绿），未到阶段待办（○ 灰）
        reached = False
        for key, _ in self._stages:
            dot = self._stage_labels[key]
            if key == stage:
                dot.setText("●")
                dot.setStyleSheet("font-size: 16px; color: #FF9500;")
                self._stage_bars[key].setValue(pct)
                reached = True
            elif not reached:
                dot.setText("●")
                dot.setStyleSheet("font-size: 16px; color: #30D158;")
                self._stage_bars[key].setValue(100)
            else:
                dot.setText("○")
                dot.setStyleSheet("font-size: 16px; color: #6B7280;")
                self._stage_bars[key].setValue(0)
        self._detail.setText(detail)
        self._log_lines.append(f"[{stage}] {detail}")
        self._log.setText("\n".join(self._log_lines[-8:]))

    def on_done(self, msg: str) -> None:
        # 全部阶段标记完成
        for key, _ in self._stages:
            self._stage_labels[key].setText("●")
            self._stage_labels[key].setStyleSheet("font-size: 16px; color: #30D158;")
            self._stage_bars[key].setValue(100)
        self._detail.setText(msg)
        self._log_lines.append(f"[done] {msg}")
        self._log.setText("\n".join(self._log_lines[-8:]))
        self._cancel_btn.setEnabled(False)
        self._done_btn.setEnabled(True)
        self._done_btn.setDefault(True)
        self._done_btn.setFocus()

    def on_failed(self, msg: str) -> None:
        self._detail.setText("安装失败：" + msg)
        self._detail.setStyleSheet("color: #FF453A; font-size: 12px;")
        self._log_lines.append(f"[error] {msg}")
        self._log.setText("\n".join(self._log_lines[-8:]))
        self._cancel_btn.setText("关闭")
        self._cancel_btn.setEnabled(True)
        try:
            self._cancel_btn.clicked.disconnect()
        except RuntimeError:
            pass
        self._cancel_btn.clicked.connect(self.reject)
        self._done_btn.setEnabled(False)
