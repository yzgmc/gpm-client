"""主窗口：整合包 / 模组 / 设置 三个标签页。"""

from __future__ import annotations

import os
import threading

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from gpm_common import GameAdapterRegistry

from app.config import ClientConfig, load_installed, save_installed
from app.downloader import download_file
from app.installer import install_modpack, install_mod
from app.launcher import launch
from app.sync_manager import SyncManager
from app.ui.widgets import DownloadProgressDialog, human_size, show_error, show_info


class SyncThread(QThread):
    """后台同步线程。"""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, manager: SyncManager) -> None:
        super().__init__()
        self.manager = manager

    def run(self) -> None:
        try:
            data = self.manager.sync()
            self.done.emit(data)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


class MainWindow(QMainWindow):
    # 跨线程信号：下载工作线程通过这些信号把更新投递到主线程（AutoConnection
    # 跨线程时自动用 QueuedConnection，由主线程事件循环处理；QDialog.exec()
    # 的嵌套事件循环能正确接收 queued 事件，从而实时刷新进度条）。
    _sig_progress = Signal(int, int)        # (downloaded, total)
    _sig_status = Signal(str)               # 对话框状态文本
    _sig_close_dialog = Signal(int)         # 0=accept, 1=reject
    _sig_statusbar = Signal(str, int)       # (消息, 毫秒)
    _sig_fail = Signal(str, str)            # (标题, 详情)

    def __init__(self, config: ClientConfig | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Game Push Manager · 客户端")
        self.resize(900, 600)

        self.config = config or ClientConfig.load()
        self.manager = SyncManager(self.config)
        self._download_dialog: DownloadProgressDialog | None = None
        self._cancel_event = threading.Event()

        # 连接跨线程信号 → 主线程槽函数
        self._sig_progress.connect(self._ui_on_progress)
        self._sig_status.connect(self._ui_set_status)
        self._sig_close_dialog.connect(self._ui_close_dialog)
        self._sig_statusbar.connect(lambda msg, ms: self.statusBar().showMessage(msg, ms))
        self._sig_fail.connect(lambda title, text: show_error(self, title, text))

        tabs = QTabWidget()
        tabs.addTab(self._build_modpack_tab(), "整合包")
        tabs.addTab(self._build_mod_tab(), "模组")
        tabs.addTab(self._build_settings_tab(), "设置")
        self.setCentralWidget(tabs)

        self._status = self.statusBar().showMessage("就绪")

        # Push 模型：启动后台心跳上报线程（若配置了 admin_url）
        from app.reporter import start_reporter

        start_reporter(self.config)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        # 窗口关闭时停止上报线程
        from app.reporter import stop_reporter

        stop_reporter()
        super().closeEvent(event)

    # ---------------- 整合包页 ----------------

    def _build_modpack_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        toolbar = QHBoxLayout()
        btn_sync = QPushButton("同步条目")
        btn_sync.clicked.connect(self._on_sync)
        toolbar.addWidget(btn_sync)
        toolbar.addStretch()
        self._mp_count = QLabel("未同步")
        toolbar.addWidget(self._mp_count)
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Horizontal)
        self._mp_list = QListWidget()
        self._mp_list.currentItemChanged.connect(self._on_mp_select)
        splitter.addWidget(self._mp_list)

        detail = QWidget()
        d_layout = QVBoxLayout(detail)
        self._mp_detail = QTextEdit()
        self._mp_detail.setReadOnly(True)
        d_layout.addWidget(self._mp_detail)

        btn_row = QHBoxLayout()
        self._btn_download_mp = QPushButton("下载 / 更新")
        self._btn_download_mp.clicked.connect(self._on_download_modpack)
        self._btn_launch = QPushButton("启动游戏")
        self._btn_launch.clicked.connect(self._on_launch)
        btn_row.addWidget(self._btn_download_mp)
        btn_row.addWidget(self._btn_launch)
        btn_row.addStretch()
        d_layout.addLayout(btn_row)

        splitter.addWidget(detail)
        splitter.setSizes([300, 600])
        layout.addWidget(splitter)
        return w

    # ---------------- 模组页 ----------------

    def _build_mod_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        toolbar = QHBoxLayout()
        btn_sync = QPushButton("同步条目")
        btn_sync.clicked.connect(self._on_sync)
        toolbar.addWidget(btn_sync)
        toolbar.addStretch()
        self._mod_count = QLabel("未同步")
        toolbar.addWidget(self._mod_count)
        layout.addLayout(toolbar)

        self._mod_table = QTableWidget(0, 6)
        self._mod_table.setHorizontalHeaderLabels(["名称", "版本", "游戏", "大小", "状态", "操作"])
        self._mod_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._mod_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._mod_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        layout.addWidget(self._mod_table)
        return w

    # ---------------- 设置页 ----------------

    def _build_settings_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)

        self._edit_server = QLineEdit(self.config.server_url)
        self._edit_install = QLineEdit(self.config.install_base_dir)
        self._edit_java = QLineEdit(self.config.java_path)
        self._edit_jvm = QLineEdit(" ".join(self.config.jvm_args))
        self._edit_admin = QLineEdit(self.config.admin_url)
        self._edit_admin.setPlaceholderText("留空则用服务端地址上报，或填独立后台地址")

        btn_install = QPushButton("浏览…")
        btn_install.clicked.connect(self._pick_install_dir)
        btn_java = QPushButton("浏览…")
        btn_java.clicked.connect(self._pick_java)

        row_install = QHBoxLayout()
        row_install.addWidget(self._edit_install)
        row_install.addWidget(btn_install)
        row_java = QHBoxLayout()
        row_java.addWidget(self._edit_java)
        row_java.addWidget(btn_java)

        form.addRow("服务端地址", self._edit_server)
        form.addRow("安装根目录", row_install)
        form.addRow("Java 路径", row_java)
        form.addRow("JVM 参数", self._edit_jvm)
        form.addRow("后台地址 (上报)", self._edit_admin)

        btn_save = QPushButton("保存设置")
        btn_save.clicked.connect(self._save_settings)
        form.addRow(btn_save)

        self._settings_label = QLabel("")
        form.addRow(self._settings_label)
        return w

    def _pick_install_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择安装根目录", self._edit_install.text())
        if d:
            self._edit_install.setText(d)

    def _pick_java(self) -> None:
        from PySide6.QtCore import QFileInfo

        f, _ = QFileDialog.getOpenFileName(self, "选择 java 可执行文件", "", "可执行文件 (*.exe);;所有文件 (*)")
        if f:
            self._edit_java.setText(f)

    def _save_settings(self) -> None:
        old_server = self.config.server_url
        self.config.server_url = self._edit_server.text().strip()
        self.config.install_base_dir = self._edit_install.text().strip()
        self.config.java_path = self._edit_java.text().strip()
        self.config.jvm_args = self._edit_jvm.text().split()
        new_admin_url = self._edit_admin.text().strip()
        admin_changed = new_admin_url != self.config.admin_url
        server_changed = self.config.server_url != old_server
        self.config.admin_url = new_admin_url
        self.config.save()
        self.manager.update_server(self.config.server_url)
        # 后台地址或服务端地址变更时重启 reporter（admin_url 为空时用 server_url 兜底）
        if admin_changed or server_changed:
            from app.reporter import start_reporter, stop_reporter

            stop_reporter()
            start_reporter(self.config)
            if self.config.admin_url:
                self._settings_label.setText("已保存，已开始向独立后台上报心跳。")
            else:
                self._settings_label.setText("已保存，已用服务端地址上报心跳。")
        else:
            self._settings_label.setText("已保存。")
        self.statusBar().showMessage("设置已保存", 3000)

    # ---------------- 同步 ----------------

    def _on_sync(self) -> None:
        self.statusBar().showMessage("正在同步…")
        self._sync_thread = SyncThread(self.manager)
        self._sync_thread.done.connect(self._on_sync_done)
        self._sync_thread.failed.connect(self._on_sync_failed)
        self._sync_thread.start()

    def _on_sync_done(self, data) -> None:
        self._mp_list.clear()
        for mp in data.modpacks:
            status = self.manager.modpack_status(mp.model_dump())
            label = f"[{status.state}] {mp.name} v{mp.version} ({mp.game} {mp.game_version} / {mp.mod_loader})"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, mp.model_dump())
            self._mp_list.addItem(item)
        self._mp_count.setText(f"整合包 {len(data.modpacks)} 个")

        mods = [m.model_dump() for m in data.mods]
        self._mod_table.setRowCount(len(mods))
        for i, m in enumerate(mods):
            status = self.manager.mod_status(m)
            self._mod_table.setItem(i, 0, QTableWidgetItem(m["name"]))
            self._mod_table.setItem(i, 1, QTableWidgetItem(m["version"]))
            self._mod_table.setItem(i, 2, QTableWidgetItem(m["game"]))
            self._mod_table.setItem(i, 3, QTableWidgetItem(human_size(m["file_size"])))
            self._mod_table.setItem(i, 4, QTableWidgetItem(status.state))
            dl_btn = QPushButton("下载")
            dl_btn.clicked.connect(lambda _=False, mid=m["id"]: self._on_download_mod(mid))
            self._mod_table.setCellWidget(i, 5, dl_btn)
        self._mod_count.setText(f"模组 {len(mods)} 个")

        self.statusBar().showMessage(f"同步完成：{len(data.modpacks)} 整合包 / {len(mods)} 模组", 5000)

    def _on_sync_failed(self, err: str) -> None:
        self.statusBar().showMessage("同步失败", 5000)
        show_error(self, "同步失败", err)

    def _on_mp_select(self, current, _previous) -> None:
        if current is None:
            self._mp_detail.setPlainText("")
            return
        mp = current.data(Qt.UserRole)
        lines = [
            f"名称: {mp['name']}",
            f"版本: {mp['version']}",
            f"游戏: {mp['game']} {mp['game_version']}",
            f"加载器: {mp['mod_loader']} {mp.get('mod_loader_version') or ''}",
            f"文件: {mp['file_name']} ({human_size(mp['file_size'])})",
            f"SHA256: {mp['file_hash'][:16]}…",
            f"描述: {mp.get('description', '')}",
            f"ID: {mp['id']}",
        ]
        self._mp_detail.setPlainText("\n".join(lines))

    # ---------------- 下载整合包 ----------------

    def _on_download_modpack(self) -> None:
        item = self._mp_list.currentItem()
        if not item:
            show_info(self, "提示", "请先选择一个整合包")
            return
        mp = item.data(Qt.UserRole)
        self._start_download("modpacks", mp)

    def _on_download_mod(self, mod_id: str) -> None:
        if not self.manager.last_sync:
            show_info(self, "提示", "请先同步")
            return
        mod = next((m.model_dump() for m in self.manager.last_sync.mods if m.id == mod_id), None)
        if not mod:
            return
        self._start_download("mods", mod)

    def _start_download(self, kind: str, item: dict) -> None:
        self._cancel_event.clear()
        self._download_dialog = DownloadProgressDialog(f"下载 {item['name']}", self)
        self._download_dialog.canceled.connect(lambda: self._cancel_event.set())

        t = threading.Thread(target=self._download_worker, args=(kind, item), daemon=True)
        t.start()
        self._download_dialog.exec()

    def _download_worker(self, kind: str, item: dict) -> None:
        try:
            url = self.manager.client.download_url(kind, item["id"])
            local_dir = os.path.join(self.config.install_base_dir, ".cache", kind, item["id"])
            dest = os.path.join(local_dir, item["file_name"])
            self._sig_status.emit("下载中…")

            download_file(
                url,
                dest,
                expected_hash=item["file_hash"],
                progress=lambda d, t: self._sig_progress.emit(d, t),
                cancel_event=self._cancel_event,
            )

            if kind == "modpacks":
                self._sig_status.emit("解压安装中…")
                adapter = GameAdapterRegistry.get(item["game"])
                if adapter:
                    install_dir = adapter.install_dir_hint(self.config.install_base_dir, item)
                else:
                    install_dir = os.path.join(self.config.install_base_dir, item["game"], item["name"])
                install_modpack(dest, install_dir)
            else:
                # 模组：安装到所属整合包目录的 mods/
                if item.get("modpack_id"):
                    mp = next(
                        (m.model_dump() for m in self.manager.last_sync.modpacks if m.id == item["modpack_id"]),
                        None,
                    )
                    if mp:
                        install_mod(dest, mp, self.config.install_base_dir)

            # 记录本地安装状态
            installed = load_installed()
            installed[item["id"]] = {
                "kind": kind,
                "hash": item["file_hash"],
                "version": item["version"],
                "name": item["name"],
            }
            save_installed(installed)

            self._sig_status.emit("完成")
            self._sig_close_dialog.emit(0)
            self._sig_statusbar.emit(f"{item['name']} 安装完成", 5000)
        except RuntimeError as e:
            # 取消或超时：静默关闭对话框，取消不弹错误框
            if self._cancel_event.is_set() or "取消" in str(e):
                self._sig_close_dialog.emit(1)
                self._sig_statusbar.emit("已取消下载", 3000)
            else:
                self._sig_fail.emit("下载失败", f"{type(e).__name__}: {e}")
                self._sig_close_dialog.emit(1)
        except Exception as e:  # noqa: BLE001
            self._sig_fail.emit("下载失败", f"{type(e).__name__}: {e}")
            self._sig_close_dialog.emit(1)

    # ---------- 主线程槽函数（由跨线程信号触发）----------
    def _ui_on_progress(self, downloaded: int, total: int) -> None:
        if self._download_dialog:
            self._download_dialog.update_progress(downloaded, total)

    def _ui_set_status(self, text: str) -> None:
        if self._download_dialog:
            self._download_dialog.set_status(text)

    def _ui_close_dialog(self, mode: int) -> None:
        if not self._download_dialog:
            return
        if mode == 0:
            self._download_dialog.accept()
        else:
            self._download_dialog.reject()

    # ---------------- 启动 ----------------

    def _on_launch(self) -> None:
        item = self._mp_list.currentItem()
        if not item:
            show_info(self, "提示", "请先选择一个整合包")
            return
        mp = item.data(Qt.UserRole)
        installed = load_installed()
        if mp["id"] not in installed:
            show_info(self, "提示", "该整合包尚未下载，请先下载")
            return
        try:
            adapter = GameAdapterRegistry.require(mp["game"])
            install_dir = adapter.install_dir_hint(self.config.install_base_dir, mp)
            proc = launch(
                game=mp["game"],
                install_dir=install_dir,
                modpack_meta=mp,
                java_path=self.config.java_path or None,
                jvm_args=self.config.jvm_args,
            )
            self.statusBar().showMessage(f"已启动 {mp['name']} (PID {proc.pid})", 5000)
        except Exception as e:  # noqa: BLE001
            show_error(self, "启动失败", f"{type(e).__name__}: {e}")
