"""主窗口：整合包 / 模组 / 设置 三个标签页。"""

from __future__ import annotations

import os
import threading

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMenuBar,
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
from app.ui.version_dialogs import CreateVersionDialog, EditVersionDialog
from app.ui.widgets import (
    DownloadProgressDialog,
    LoaderInstallDialog,
    ask_yes,
    human_size,
    show_error,
    show_info,
)
from app.version_manager import (
    VersionInstance,
    create_version as vm_create_version,
    delete_version as vm_delete_version,
    fetch_mc_version_ids,
    game_root as vm_game_root,
    list_versions as vm_list_versions,
    resolve_game_dir as vm_resolve_game_dir,
    touch_last_played as vm_touch_last_played,
    update_instance_config as vm_update_instance_config,
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
    # 版本管理：MC 版本清单异步填充信号（后台线程 → 主线程更新对话框下拉）
    _sig_ver_versions = Signal(list)              # (list[str] 版本 id)

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
        self._ver_create_dlg: CreateVersionDialog | None = None  # 新建版本对话框（版本清单异步回填用）

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
        # 版本管理：版本清单异步回填
        self._sig_ver_versions.connect(self._ui_ver_versions)

        tabs = QTabWidget()
        tabs.addTab(self._build_modpack_tab(), "整合包")
        tabs.addTab(self._build_mod_tab(), "模组")
        tabs.addTab(self._build_versions_tab(), "版本管理")
        tabs.addTab(self._build_settings_tab(), "设置")
        self._tabs = tabs
        tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(tabs)

        self._build_menu()

        self._status = self.statusBar().showMessage("就绪")

        # Push 模型：启动后台心跳上报线程（若配置了 admin_url）
        from app.reporter import start_reporter

        start_reporter(self.config)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        # 窗口关闭时停止上报线程
        from app.reporter import stop_reporter

        stop_reporter()
        super().closeEvent(event)

    # ---------------- 菜单栏 ----------------

    def _build_menu(self) -> None:
        """构建顶部菜单栏。"""
        mb = self.menuBar()
        account_menu = mb.addMenu("账号(&A)")

        self._act_msa_login = QAction("微软账号登录…", self)
        self._act_msa_login.triggered.connect(self._on_msa_login)
        account_menu.addAction(self._act_msa_login)

        self._act_msa_logout = QAction("退出微软账号", self)
        self._act_msa_logout.triggered.connect(self._on_msa_logout)
        self._act_msa_logout.setEnabled(False)
        account_menu.addAction(self._act_msa_logout)

        account_menu.addSeparator()
        self._act_msa_status = QAction("当前：离线模式", self)
        self._act_msa_status.setEnabled(False)
        account_menu.addAction(self._act_msa_status)

        # 初始刷新菜单状态
        self._refresh_msa_menu()

    def _refresh_msa_menu(self) -> None:
        """根据 config.msa_credentials 刷新菜单显示。"""
        from app.msa_auth import MsaCredentials

        if self.config.msa_credentials:
            creds = MsaCredentials.from_dict(self.config.msa_credentials)
            self._act_msa_status.setText(f"当前：{creds.username}（正版）")
            self._act_msa_login.setText("切换微软账号…")
            self._act_msa_logout.setEnabled(True)
        else:
            self._act_msa_status.setText("当前：离线模式")
            self._act_msa_login.setText("微软账号登录…")
            self._act_msa_logout.setEnabled(False)

    def _on_msa_login(self) -> None:
        """微软账号登录：后台线程跑 OAuth 流程，避免阻塞 UI。"""
        from app.msa_auth import login_with_browser, MsaCredentials

        # 用进度对话框提示用户正在登录
        from PySide6.QtWidgets import QProgressDialog

        progress = QProgressDialog("正在登录微软账号，请在弹出的浏览器中完成登录…", "取消", 0, 0, self)
        progress.setWindowTitle("微软账号登录")
        progress.setModal(True)
        progress.setMinimumDuration(0)

        result: dict = {"creds": None, "error": None}
        done_event = threading.Event()

        def worker() -> None:
            try:
                creds = login_with_browser()
                result["creds"] = creds
            except Exception as e:  # noqa: BLE001
                result["error"] = f"{type(e).__name__}: {e}"
            finally:
                done_event.set()

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        # 轮询等待完成（让 UI 保持响应）
        while not done_event.is_set():
            QApplication.processEvents()
            if progress.wasCanceled():
                # 取消只是关闭提示，OAuth 流程已在后台跑，用户可关浏览器放弃
                break
            done_event.wait(0.1)

        progress.close()

        if result["creds"]:
            creds: MsaCredentials = result["creds"]
            self.config.msa_credentials = creds.to_dict()
            self.config.save()
            self._refresh_msa_menu()
            show_info(self, "登录成功", f"已登录微软账号：{creds.username}\n后续启动游戏将使用正版账号。")
        elif result["error"]:
            show_error(self, "微软账号登录失败", str(result["error"]))

    def _on_msa_logout(self) -> None:
        """退出微软账号，回到离线模式。"""
        if not self.config.msa_credentials:
            return
        if not ask_yes(self, "确认退出", "确定要退出当前微软账号吗？退出后将用离线模式启动游戏。"):
            return
        self.config.msa_credentials = {}
        self.config.save()
        self._refresh_msa_menu()
        show_info(self, "已退出", "已退出微软账号，启动游戏将用离线模式。")

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

    # ---------------- 版本管理页 ----------------

    def _build_versions_tab(self) -> QWidget:
        """版本管理：列出共享游戏根目录下所有版本，可单独启动/配置/删除。

        每个版本有独立的 gpm_instance.json（显示名、Java、JVM 参数、隔离开关），
        存档/模组/配置按版本隔离，互不干扰（HMCL 风格）。
        """
        w = QWidget()
        layout = QVBoxLayout(w)

        toolbar = QHBoxLayout()
        btn_new = QPushButton("新建版本")
        btn_new.clicked.connect(self._ver_on_create)
        btn_launch = QPushButton("启动")
        btn_launch.clicked.connect(self._ver_on_launch)
        btn_edit = QPushButton("配置")
        btn_edit.clicked.connect(self._ver_on_edit)
        btn_open = QPushButton("打开目录")
        btn_open.clicked.connect(self._ver_on_open_dir)
        btn_del = QPushButton("删除")
        btn_del.clicked.connect(self._ver_on_delete)
        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(self._ver_refresh)
        toolbar.addWidget(btn_new)
        toolbar.addWidget(btn_launch)
        toolbar.addWidget(btn_edit)
        toolbar.addWidget(btn_open)
        toolbar.addWidget(btn_del)
        toolbar.addStretch()
        toolbar.addWidget(btn_refresh)
        layout.addLayout(toolbar)

        self._ver_list = QListWidget()
        self._ver_list.doubleClicked.connect(lambda *_: self._ver_on_launch())
        layout.addWidget(self._ver_list)

        hint = QLabel(
            "版本管理：在共享目录下集中管理多个 Minecraft 版本，每个版本可独立启动，互不干扰。\n"
            "各版本共享 libraries/assets（省磁盘），存档/模组/配置默认按版本隔离。"
        )
        hint.setStyleSheet("color: #8E8E93; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # 首次填充
        self._ver_refresh()
        return w

    def _on_tab_changed(self, index: int) -> None:
        """切换到版本管理页时自动刷新列表。"""
        if self._tabs.tabText(index) == "版本管理":
            self._ver_refresh()

    def _ver_refresh(self) -> None:
        """扫描共享游戏根目录，刷新版本列表。"""
        if not hasattr(self, "_ver_list"):
            return
        root = vm_game_root(self.config.install_base_dir)
        versions = vm_list_versions(root)
        self._ver_list.clear()
        if not versions:
            item = QListWidgetItem("（暂无版本，点击「新建版本」创建）")
            item.setData(Qt.UserRole, None)
            self._ver_list.addItem(item)
            return
        for inst in versions:
            status = "就绪" if inst.ready else "未完成"
            iso = "隔离" if inst.isolated else "共享"
            last = inst.last_played[:16].replace("T", " ") if inst.last_played else "未启动"
            label = (
                f"{inst.effective_display_name}  "
                f"[{inst.mod_loader}"
                + (f" {inst.mod_loader_version}" if inst.mod_loader_version else "")
                + f" / MC {inst.game_version or '未知'}]  "
                f"· {status} · {iso} · 上次：{last}"
            )
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, inst)
            self._ver_list.addItem(item)

    def _ver_current(self) -> VersionInstance | None:
        if not hasattr(self, "_ver_list"):
            return None
        item = self._ver_list.currentItem()
        if not item:
            return None
        data = item.data(Qt.UserRole)
        return data if isinstance(data, VersionInstance) else None

    def _ver_on_create(self) -> None:
        """新建版本：弹对话框收集参数 → 后台拉原版文件+装加载器 → 写独立配置。"""
        dlg = CreateVersionDialog(default_java=self.config.java_path, parent=self)
        # 后台拉取 MC 版本清单填充下拉
        self._ver_fill_mc_versions(dlg)
        accepted = dlg.exec() == QDialog.Accepted
        self._ver_create_dlg = None  # 清理，避免后续误填已关闭的对话框
        if not accepted:
            return
        vals = dlg.values()
        mc_version = vals["game_version"]
        loader = vals["mod_loader"]
        loader_version = vals["mod_loader_version"]
        display_name = vals["display_name"]
        isolated = vals["isolated"]
        java_path = vals["java_path"] or self.config.java_path

        # 安装加载器需要 Java：若仍缺失则尝试自动下载
        if loader != "vanilla" and not (java_path and _is_usable_java(java_path)):
            if not self._ensure_java(mc_version):
                return
            java_path = self.config.java_path

        stages = [("download", "下载原版文件"), ("install", "安装加载器"), ("done", "完成")]
        self._loader_cancel.clear()
        self._loader_dialog = LoaderInstallDialog(
            f"新建版本 · MC {mc_version} {loader.capitalize()}", loader.capitalize(), self, stages=stages
        )
        self._loader_dialog.canceled.connect(lambda: self._loader_cancel.set())

        result: dict = {"vid": None}

        def worker() -> None:
            try:
                vid = vm_create_version(
                    install_base_dir=self.config.install_base_dir,
                    game_version=mc_version,
                    loader=loader,
                    loader_version=loader_version,
                    display_name=display_name,
                    java_path=java_path,
                    isolated=isolated,
                    progress=lambda stage, detail, pct: self._sig_loader_progress.emit(stage, detail, pct),
                    cancel_event=self._loader_cancel,
                )
                result["vid"] = vid
                self._sig_loader_done.emit(f"版本 {vid} 已创建")
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
        # 完成后刷新列表（无论成功失败，都可能已建出原版目录）
        self._ver_refresh()
        if result["vid"]:
            show_info(self, "创建成功", f"版本已创建：{result['vid']}\n可在列表选中后点击「启动」。")

    def _ver_fill_mc_versions(self, dlg: CreateVersionDialog) -> None:
        """后台拉取 MC 版本清单，完成后通过信号异步回填对话框下拉。

        不阻塞：对话框立即弹出（下拉显示"正在加载版本列表…"），
        拉取完成后 _sig_ver_versions 信号在主线程把版本填入（exec 期间也能收到）。
        """
        self._ver_create_dlg = dlg

        def worker() -> None:
            versions = fetch_mc_version_ids(release_only=True)
            self._sig_ver_versions.emit(versions)

        threading.Thread(target=worker, daemon=True).start()

    def _ui_ver_versions(self, versions: list) -> None:
        """主线程槽：收到后台版本清单后回填新建版本对话框下拉。"""
        if self._ver_create_dlg is not None:
            self._ver_create_dlg.set_mc_versions(list(versions))

    def _ver_on_launch(self) -> None:
        """启动选中版本：确保 Java + 原版文件 → 解析账号 → 按 version_id 与隔离 game_dir 启动。"""
        inst = self._ver_current()
        if inst is None:
            show_info(self, "提示", "请先选择一个版本")
            return
        mc_version = inst.game_version or ""
        # 1. 确保 Java
        if mc_version:
            if not self._ensure_java(mc_version):
                return
        java_path = inst.java_path or self.config.java_path
        # 2. 确保原版文件齐全（在共享根目录）
        if mc_version and not inst.ready:
            if not self._ver_ensure_files(inst):
                return
        # 3. 解析账号
        account, aborted = self._resolve_launch_account()
        if aborted:
            return
        # 4. 启动
        try:
            root = vm_game_root(self.config.install_base_dir)
            game_dir = vm_resolve_game_dir(root, inst)
            modpack_meta = {
                "game_version": mc_version,
                "mod_loader": inst.mod_loader,
                "mod_loader_version": inst.mod_loader_version or "",
                "version_id": inst.version_id,
            }
            proc = launch(
                game="minecraft",
                install_dir=root,
                modpack_meta=modpack_meta,
                java_path=java_path or None,
                jvm_args=inst.jvm_args or self.config.jvm_args,
                account=account,
                game_dir=game_dir,
            )
            vm_touch_last_played(inst.version_dir)
            mode = "正版" if account else "离线"
            iso = "隔离" if inst.isolated else "共享"
            self.statusBar().showMessage(
                f"已启动 {inst.effective_display_name}（{mode}模式·{iso}，PID {proc.pid}）", 5000
            )
            self._ver_refresh()
        except Exception as e:  # noqa: BLE001
            show_error(self, "启动失败", f"{type(e).__name__}: {e}")

    def _ver_ensure_files(self, inst: VersionInstance) -> bool:
        """版本未就绪时下载缺失的原版文件到共享根目录。返回是否就绪。"""
        from app.minecraft_installer import ensure_vanilla_version, is_vanilla_version_ready

        mc_version = inst.game_version
        if not mc_version:
            show_error(self, "无法补全", "该版本未识别到游戏版本，无法自动下载原版文件。")
            return False
        root = vm_game_root(self.config.install_base_dir)
        if is_vanilla_version_ready(root, mc_version):
            return True
        stages = [("download", "下载原版文件"), ("done", "完成")]
        self._loader_cancel.clear()
        self._loader_dialog = LoaderInstallDialog(
            f"补全原版文件 · MC {mc_version}", "原版游戏文件", self, stages=stages
        )
        self._loader_dialog.canceled.connect(lambda: self._loader_cancel.set())
        result: dict = {"ok": False}

        def worker() -> None:
            try:
                ensure_vanilla_version(
                    install_dir=root,
                    mc_version=mc_version,
                    progress=lambda stage, detail, pct: self._sig_loader_progress.emit(stage, detail, pct),
                    cancel_event=self._loader_cancel,
                )
                result["ok"] = True
                self._sig_loader_done.emit(f"Minecraft {mc_version} 原版文件就绪")
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
        return result["ok"]

    def _ver_on_edit(self) -> None:
        """编辑选中版本的独立配置（显示名/Java/JVM/隔离）。"""
        inst = self._ver_current()
        if inst is None:
            show_info(self, "提示", "请先选择一个版本")
            return
        global_jvm = " ".join(self.config.jvm_args)
        dlg = EditVersionDialog(
            inst,
            global_java=self.config.java_path,
            global_jvm=global_jvm,
            parent=self,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        vals = dlg.values()
        vm_update_instance_config(
            inst.version_dir,
            display_name=vals["display_name"],
            java_path=vals["java_path"],
            jvm_args=vals["jvm_args"],
            isolated=vals["isolated"],
        )
        self._ver_refresh()
        show_info(self, "已保存", f"版本配置已更新：{inst.effective_display_name}")

    def _ver_on_delete(self) -> None:
        """删除选中版本目录（仅删该版本，不影响共享库与其它版本）。"""
        inst = self._ver_current()
        if inst is None:
            show_info(self, "提示", "请先选择一个版本")
            return
        if not ask_yes(
            self, "确认删除",
            f"确定删除版本「{inst.effective_display_name}」吗？\n\n"
            f"将删除目录：{inst.version_dir}\n"
            "（共享的 libraries/assets 与其它版本不受影响）\n"
            "若该版本是原版且被其它加载器版本依赖，删除后那些版本将无法启动。",
        ):
            return
        try:
            root = vm_game_root(self.config.install_base_dir)
            vm_delete_version(root, inst.version_id)
            self._ver_refresh()
            show_info(self, "已删除", f"版本已删除：{inst.effective_display_name}")
        except Exception as e:  # noqa: BLE001
            show_error(self, "删除失败", f"{type(e).__name__}: {e}")

    def _ver_on_open_dir(self) -> None:
        """在文件管理器中打开版本目录。"""
        inst = self._ver_current()
        if inst is None:
            show_info(self, "提示", "请先选择一个版本")
            return
        path = inst.version_dir
        if not os.path.isdir(path):
            show_info(self, "提示", "版本目录不存在")
            return
        self._open_in_explorer(path)

    @staticmethod
    def _open_in_explorer(path: str) -> None:
        import sys

        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", path])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path])
        except Exception:
            pass

    # ---------------- 设置页 ----------------

    def _build_settings_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)

        self._edit_server = QLineEdit(self.config.server_url)
        self._edit_install = QLineEdit(self.config.install_base_dir)
        self._edit_java = QLineEdit(self.config.java_path)
        self._edit_jvm = QLineEdit(" ".join(self.config.jvm_args))
        self._edit_jvm.setPlaceholderText("留空则启动时自动分配内存并应用 JVM 优化参数")
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

    # ---------------- 原版 MC 文件下载 ----------------

    def _ensure_vanilla_version(self, item: dict) -> bool:
        """确保原版 MC 版本文件齐全（client jar + libraries + assets）。

        Fabric/Quilt/Forge/NeoForge 安装器只装加载器本身，不下载原版游戏文件。
        缺少原版 jar 时 Fabric 会报 "Minecraft game provider couldn't locate the game!"。

        返回 True 表示就绪（已存在或下载成功）；False 表示失败或取消。
        """
        from app.minecraft_installer import ensure_vanilla_version, is_vanilla_version_ready

        mc_version = item.get("game_version") or ""
        if not mc_version:
            return True  # 无版本号，无法处理，交给后续启动逻辑报错

        install_dir = self._modpack_install_dir(item)

        # 快速检查：client jar 已存在则视为就绪，不弹窗
        if is_vanilla_version_ready(install_dir, mc_version):
            return True

        stages = [("download", "下载原版文件"), ("done", "完成")]
        self._loader_cancel.clear()
        self._loader_dialog = LoaderInstallDialog(
            f"下载 Minecraft {mc_version}", "原版游戏文件", self, stages=stages
        )
        self._loader_dialog.canceled.connect(lambda: self._loader_cancel.set())

        result = {"ok": False}

        def worker() -> None:
            try:
                ensure_vanilla_version(
                    install_dir=install_dir,
                    mc_version=mc_version,
                    progress=lambda stage, detail, pct: self._sig_loader_progress.emit(stage, detail, pct),
                    cancel_event=self._loader_cancel,
                )
                result["ok"] = True
                self._sig_loader_done.emit(f"Minecraft {mc_version} 原版文件就绪")
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
        return result["ok"]

    # ---------------- 加载器自动安装 ----------------

    def _modpack_install_dir(self, item: dict) -> str:
        """计算整合包解压后的安装目录（与 _download_worker 保持一致）。"""
        adapter = GameAdapterRegistry.get(item["game"])
        if adapter:
            return adapter.install_dir_hint(self.config.install_base_dir, item)
        return os.path.join(self.config.install_base_dir, item["game"], item["name"])

    def _maybe_install_loader(self, item: dict) -> None:
        """整合包下载完成后，按 mod_loader 自动弹出多阶段安装窗口。

        流程：确保 Java → 下载原版 MC 文件 → 安装加载器（vanilla 跳过加载器）。
        vanilla 不需要安装器，但仍需确保 Java 和原版文件齐全。
        """
        loader = (item.get("mod_loader") or "vanilla").lower()
        mc_version = item.get("game_version") or ""

        # 所有加载器（含 vanilla）启动游戏都需要 Java，先确保 Java 可用
        if mc_version:
            if not self._ensure_java(mc_version):
                return  # Java 未就绪或被取消，中止后续安装

        # 下载原版 MC 文件（client jar + libraries + assets），所有加载器都需要
        if mc_version:
            if not self._ensure_vanilla_version(item):
                return  # 原版文件未就绪或被取消

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

    def _resolve_launch_account(self) -> tuple[dict | None, bool]:
        """启动前解析正版账号。

        返回 (account, aborted)：
        - (None, False)      未登录微软账号 → 离线模式启动
        - (creds, False)     已登录且凭据有效 → 正版启动
        - (None, True)       用户取消 → 中止启动

        续登失败时询问用户：重新浏览器登录 / 离线启动 / 取消。
        account dict 含 username/uuid/mc_access_token，供 launch(account=...) 使用。
        """
        from app.msa_auth import MsaCredentials, ensure_valid_credentials

        if not self.config.msa_credentials:
            return None, False

        creds = MsaCredentials.from_dict(self.config.msa_credentials)
        if creds.is_mc_token_valid:
            return creds.to_dict(), False

        # MC token 过期：后台静默续登，避免阻塞 UI
        from PySide6.QtWidgets import QProgressDialog

        progress = QProgressDialog("正在续登微软账号…", "取消", 0, 0, self)
        progress.setWindowTitle("微软账号续登")
        progress.setModal(True)
        progress.setMinimumDuration(0)

        result: dict = {"creds": None, "error": None}
        done_event = threading.Event()

        def worker() -> None:
            try:
                new_creds = ensure_valid_credentials(creds)
                result["creds"] = new_creds
            except Exception as e:  # noqa: BLE001
                result["error"] = f"{type(e).__name__}: {e}"
            finally:
                done_event.set()

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        while not done_event.is_set():
            QApplication.processEvents()
            if progress.wasCanceled():
                break
            done_event.wait(0.1)
        progress.close()

        if result["creds"]:
            new_creds: MsaCredentials = result["creds"]
            # refresh_token 每次刷新都会轮换，必须持久化最新的
            self.config.msa_credentials = new_creds.to_dict()
            self.config.save()
            self._refresh_msa_menu()
            return new_creds.to_dict(), False

        # 续登失败：询问用户后续操作
        if result["error"]:
            choice = QMessageBox.question(
                self,
                "微软账号续登失败",
                f"续登失败：{result['error']}\n\n是否重新打开浏览器登录？\n"
                "（“是”=重新登录；“否”=用离线模式启动本次；“取消”=中止启动）",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if choice == QMessageBox.Yes:
                self._on_msa_login()
                # 登录成功后用新凭据启动；失败/取消则中止
                if self.config.msa_credentials:
                    return self.config.msa_credentials, False
                return None, True
            if choice == QMessageBox.No:
                return None, False  # 离线模式启动
            return None, True  # 取消 → 中止启动
        return None, False

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
        # 确保原版 MC 文件齐全（兼容旧整合包：下载前未自动拉取原版文件的情况）
        if mc_version:
            if not self._ensure_vanilla_version(mp):
                return  # 原版文件未就绪或被取消
        # 解析正版账号：已登录微软账号则用正版启动，token 过期则静默续登
        account, aborted = self._resolve_launch_account()
        if aborted:
            return  # 用户取消，中止启动
        try:
            adapter = GameAdapterRegistry.require(mp["game"])
            install_dir = adapter.install_dir_hint(self.config.install_base_dir, mp)
            proc = launch(
                game=mp["game"],
                install_dir=install_dir,
                modpack_meta=mp,
                java_path=self.config.java_path or None,
                jvm_args=self.config.jvm_args,
                account=account,
            )
            mode = "正版" if account else "离线"
            self.statusBar().showMessage(f"已启动 {mp['name']}（{mode}模式，PID {proc.pid}）", 5000)
        except Exception as e:  # noqa: BLE001
            show_error(self, "启动失败", f"{type(e).__name__}: {e}")
