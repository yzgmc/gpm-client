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
from app.java_installer import ensure_java as ensure_java_runtime, _is_usable_java
from app.launcher import launch
from app.loader_installer import install_loader, SUPPORTED_LOADERS
from app.sync_manager import SyncManager
from app.ui.widgets import (
    DownloadProgressDialog,
    LoaderInstallDialog,
    human_size,
    show_error,
    show_info,
)


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
    # 加载器安装多阶段进度信号
    _sig_loader_progress = Signal(str, str, int)  # (stage, detail, pct)
    _sig_loader_done = Signal(str)                # (msg)
    _sig_loader_failed = Signal(str)              # (msg)

    def __init__(self, config: ClientConfig | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Game Push Manager · 客户端")
        self.resize(900, 600)

        self.config = config or ClientConfig.load()
        self.manager = SyncManager(self.config)
        self._download_dialog: DownloadProgressDialog | None = None
        self._cancel_event = threading.Event()
        self._loader_dialog: LoaderInstallDialog | None = None
        self._loader_cancel = threading.Event()

        # 连接跨线程信号 → 主线程槽函数
        self._sig_progress.connect(self._ui_on_progress)
        self._sig_status.connect(self._ui_set_status)
        self._sig_close_dialog.connect(self._ui_close_dialog)
        self._sig_statusbar.connect(lambda msg, ms: self.statusBar().showMessage(msg, ms))
        self._sig_fail.connect(lambda title, text: show_error(self, title, text))
        # 加载器安装信号 → 对话框更新
        self._sig_loader_progress.connect(self._ui_loader_progress)
        self._sig_loader_done.connect(self._ui_loader_done)
        self._sig_loader_failed.connect(self._ui_loader_failed)

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
        # 模组下载：让用户选择安装方式
        modpack_id = mod.get("modpack_id")
        if modpack_id:
            mp = next((m.model_dump() for m in self.manager.last_sync.modpacks if m.id == modpack_id), None)
        else:
            mp = None
        choice = self._choose_mod_install_mode(mod, mp)
        if choice is None:
            return  # 用户取消
        # choice: ("modpack", None) 装到整合包 mods/ ; ("saveas", "D:/path") 另存为指定目录
        self._start_download("mods", mod, mod_mode=choice)

    def _choose_mod_install_mode(self, mod: dict, modpack: dict | None) -> tuple[str, str | None] | None:
        """弹出对话框让用户选择模组安装方式。

        返回:
          ("modpack", None) - 装到所属整合包的 mods/ 目录
          ("saveas", path)  - 另存为用户选择的文件夹
          None              - 用户取消
        """
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("下载模组 - 选择安装方式")
        if modpack:
            msg_box.setText(
                f"模组：{mod['name']} v{mod['version']}\n"
                f"所属整合包：{modpack['name']} v{modpack['version']}\n\n"
                f"请选择安装方式：\n"
                f"“装到整合包”：复制到整合包的 mods/ 文件夹（需已安装该整合包）\n"
                f"“另存为”：选择一个文件夹保存模组文件"
            )
            install_btn = msg_box.addButton("装到整合包", QMessageBox.AcceptRole)
        else:
            msg_box.setText(
                f"模组：{mod['name']} v{mod['version']}\n\n"
                f"该模组未关联整合包，请选择保存位置：\n"
                f"“另存为”：选择一个文件夹保存模组文件\n"
                f"“取消”：放弃下载"
            )
            install_btn = None
        saveas_btn = msg_box.addButton("另存为", QMessageBox.AcceptRole)
        cancel_btn = msg_box.addButton("取消", QMessageBox.RejectRole)
        msg_box.exec()
        clicked = msg_box.clickedButton()

        if clicked is cancel_btn or clicked is None:
            return None
        if clicked is install_btn and modpack:
            return ("modpack", None)
        if clicked is saveas_btn:
            target = QFileDialog.getExistingDirectory(self, "选择模组保存文件夹", self.config.install_base_dir)
            if not target:
                return None
            return ("saveas", target)
        return None

    def _start_download(self, kind: str, item: dict, mod_mode: tuple[str, str | None] | None = None) -> None:
        self._cancel_event.clear()
        self._download_dialog = DownloadProgressDialog(f"下载 {item['name']}", self)
        self._download_dialog.canceled.connect(lambda: self._cancel_event.set())

        t = threading.Thread(target=self._download_worker, args=(kind, item, mod_mode), daemon=True)
        t.start()
        self._download_dialog.exec()
        accepted = self._download_dialog.result() == DownloadProgressDialog.Accepted
        self._download_dialog = None

        # 整合包下载成功后：按 mod_loader 自动弹出加载器安装窗口
        if kind == "modpacks" and accepted:
            self._maybe_install_loader(item)

    def _download_worker(self, kind: str, item: dict, mod_mode: tuple[str, str | None] | None = None) -> None:
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
                # 模组：根据用户选择的安装方式处理
                # mod_mode: ("modpack", None) 装到整合包 mods/ ; ("saveas", path) 另存为指定目录
                if mod_mode and mod_mode[0] == "saveas":
                    target_dir = mod_mode[1]
                    install_mod(dest, {}, self.config.install_base_dir, target_dir=target_dir)
                elif mod_mode and mod_mode[0] == "modpack":
                    # 装到所属整合包的 mods/
                    mp = None
                    if item.get("modpack_id") and self.manager.last_sync:
                        mp = next(
                            (m.model_dump() for m in self.manager.last_sync.modpacks if m.id == item["modpack_id"]),
                            None,
                        )
                    if mp:
                        install_mod(dest, mp, self.config.install_base_dir)
                    else:
                        self._sig_fail.emit("安装失败", "未找到所属整合包信息，无法定位 mods 目录")
                        self._sig_close_dialog.emit(1)
                        return

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

    # ---------------- Java 运行时自动安装 ----------------

    def _ensure_java(self, mc_version: str) -> str | None:
        """确保有可用的 Java 运行时。若需下载则弹多阶段进度对话框。

        返回可用的 java.exe 路径；失败或取消返回 None。
        成功下载/定位到新 Java 后会写回 config.java_path 并同步设置页输入框。
        """
        # 已配置且可用 → 直接返回，不弹窗
        if self.config.java_path and _is_usable_java(self.config.java_path):
            return self.config.java_path

        if not mc_version:
            show_info(self, "提示", "未识别到游戏版本，无法自动下载匹配的 Java，请在设置中手动指定 Java 路径。")
            return None

        stages = [("download", "下载 Java"), ("extract", "解压 Java"), ("done", "完成")]
        self._loader_cancel.clear()
        self._loader_dialog = LoaderInstallDialog(
            f"安装 Java · MC {mc_version}", "Java 运行时", self, stages=stages
        )
        self._loader_dialog.canceled.connect(lambda: self._loader_cancel.set())

        result = {"java": None}

        def worker() -> None:
            try:
                java = ensure_java_runtime(
                    mc_version=mc_version,
                    install_base_dir=self.config.install_base_dir,
                    java_path=self.config.java_path or None,
                    progress=lambda stage, detail, pct: self._sig_loader_progress.emit(stage, detail, pct),
                    cancel_event=self._loader_cancel,
                )
                result["java"] = java
                self._sig_loader_done.emit(f"Java 已就绪：{java}")
            except RuntimeError as e:
                if self._loader_cancel.is_set() or "取消" in str(e):
                    self._sig_loader_failed.emit("已取消")
                else:
                    self._sig_loader_failed.emit(str(e))
            except Exception as e:  # noqa: BLE001
                self._sig_loader_failed.emit(f"{type(e).__name__}: {e}")

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        self._loader_dialog.exec()
        self._loader_dialog = None

        java = result["java"]
        if java and java != self.config.java_path:
            self.config.java_path = java
            self.config.save()
            # 同步设置页输入框
            if hasattr(self, "_edit_java"):
                self._edit_java.setText(java)
        return java

    # ---------------- 加载器自动安装 ----------------

    def _modpack_install_dir(self, item: dict) -> str:
        """计算整合包解压后的安装目录（与 _download_worker 保持一致）。"""
        adapter = GameAdapterRegistry.get(item["game"])
        if adapter:
            return adapter.install_dir_hint(self.config.install_base_dir, item)
        return os.path.join(self.config.install_base_dir, item["game"], item["name"])

    def _maybe_install_loader(self, item: dict) -> None:
        """整合包下载完成后，按 mod_loader 自动弹出多阶段安装窗口。

        vanilla 不需要安装器，但仍需确保 Java 可用（启动游戏要用）；
        其余加载器先确保 Java，再调用 loader_installer 统一安装。
        """
        loader = (item.get("mod_loader") or "vanilla").lower()
        mc_version = item.get("game_version") or ""

        # 所有加载器（含 vanilla）启动游戏都需要 Java，先确保 Java 可用
        if mc_version:
            if not self._ensure_java(mc_version):
                return  # Java 未就绪或被取消，中止后续安装

        if loader == "vanilla":
            return
        if loader not in SUPPORTED_LOADERS:
            show_info(
                self,
                "提示",
                f"该整合包使用的加载器 “{loader}” 暂不支持自动安装，请手动安装后启动。",
            )
            return
        if not mc_version:
            show_info(self, "提示", "该整合包未识别到游戏版本，无法自动安装加载器，请手动安装。")
            return

        install_dir = self._modpack_install_dir(item)
        loader_version = item.get("mod_loader_version") or ""
        loader_name = loader.capitalize()
        self._loader_cancel.clear()
        self._loader_dialog = LoaderInstallDialog(f"安装 {loader_name} · {item['name']}", loader_name, self)
        self._loader_dialog.canceled.connect(lambda: self._loader_cancel.set())

        t = threading.Thread(
            target=self._loader_worker,
            args=(loader, install_dir, mc_version, loader_version),
            daemon=True,
        )
        t.start()
        self._loader_dialog.exec()
        self._loader_dialog = None

    def _loader_worker(self, loader: str, install_dir: str, mc_version: str, loader_version: str) -> None:
        try:
            install_loader(
                loader=loader,
                install_dir=install_dir,
                mc_version=mc_version,
                loader_version=loader_version or None,
                install_base_dir=self.config.install_base_dir,
                java_path=self.config.java_path,
                progress=lambda stage, detail, pct: self._sig_loader_progress.emit(stage, detail, pct),
                cancel_event=self._loader_cancel,
            )
            self._sig_loader_done.emit(f"{loader.capitalize()} 已安装到 {install_dir}，可启动游戏")
        except RuntimeError as e:
            if self._loader_cancel.is_set() or "取消" in str(e):
                self._sig_loader_failed.emit("已取消")
            else:
                self._sig_loader_failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            self._sig_loader_failed.emit(f"{type(e).__name__}: {e}")

    # ---------- 加载器对话框主线程槽 ----------
    def _ui_loader_progress(self, stage: str, detail: str, pct: int) -> None:
        if self._loader_dialog:
            self._loader_dialog.on_progress(stage, detail, pct)

    def _ui_loader_done(self, msg: str) -> None:
        if self._loader_dialog:
            self._loader_dialog.on_done(msg)
            self._sig_statusbar.emit("加载器安装完成", 5000)

    def _ui_loader_failed(self, msg: str) -> None:
        if self._loader_dialog:
            self._loader_dialog.on_failed(msg)

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
        # 启动前确保 Java 可用（按 MC 版本匹配，缺失则自动下载）
        mc_version = mp.get("game_version") or ""
        if mc_version:
            if not self._ensure_java(mc_version):
                return  # Java 未就绪或被取消
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
