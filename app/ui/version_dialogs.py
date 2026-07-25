"""版本管理对话框：新建版本 / 编辑版本配置。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.loader_installer import SUPPORTED_LOADERS


class CreateVersionDialog(QDialog):
    """新建版本对话框：选择 MC 版本、加载器、加载器版本、显示名、隔离开关。

    MC 版本下拉在打开时后台拉取版本清单填充（失败回退为可编辑输入）。
    """

    def __init__(self, default_java: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("新建版本")
        self.setMinimumWidth(440)

        self._mc_versions: list[str] = []

        form = QFormLayout()
        self._display = QLineEdit()
        self._display.setPlaceholderText("可选，留空则用版本号作为显示名")
        form.addRow("显示名称", self._display)

        self._mc_combo = QComboBox()
        self._mc_combo.setEditable(True)
        self._mc_combo.setPlaceholderText("正在加载版本列表…")
        form.addRow("Minecraft 版本", self._mc_combo)

        self._loader_combo = QComboBox()
        self._loader_combo.addItem("原版 (Vanilla)", "vanilla")
        for ld in SUPPORTED_LOADERS:
            self._loader_combo.addItem(ld.capitalize(), ld)
        form.addRow("模组加载器", self._loader_combo)

        self._loader_ver = QLineEdit()
        self._loader_ver.setPlaceholderText("留空则自动取最新稳定版")
        form.addRow("加载器版本", self._loader_ver)

        self._java = QLineEdit(default_java)
        self._java.setPlaceholderText("留空则用全局设置中的 Java 路径")
        btn_java = QPushButton("浏览…")
        btn_java.clicked.connect(self._pick_java)
        row_java = QHBoxLayout()
        row_java.addWidget(self._java)
        row_java.addWidget(btn_java)
        java_w = QWidget()
        java_w.setLayout(row_java)
        form.addRow("Java 路径", java_w)

        self._isolated = QCheckBox("版本隔离（存档/模组/配置独立，互不干扰）")
        self._isolated.setChecked(True)
        form.addRow("", self._isolated)

        hint = QLabel(
            "说明：各版本共享 libraries/assets（省磁盘），但存档/模组/配置按版本隔离。\n"
            "vanilla 无需 Java 即可创建；安装模组加载器需要 Java。"
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)

        btn_create = QPushButton("创建")
        btn_create.setObjectName("primary")
        btn_create.setDefault(True)
        btn_create.clicked.connect(self._on_create)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        row_btn = QHBoxLayout()
        row_btn.addStretch()
        row_btn.addWidget(btn_cancel)
        row_btn.addWidget(btn_create)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addLayout(row_btn)

    # ---------- MC 版本列表填充 ----------
    def set_mc_versions(self, versions: list[str]) -> None:
        """主窗口异步拉到版本清单后调用，填充下拉。保留用户已输入文本。"""
        self._mc_versions = list(versions)
        cur = self._mc_combo.currentText().strip()
        self._mc_combo.blockSignals(True)
        self._mc_combo.clear()
        if versions:
            self._mc_combo.addItems(versions)
            if cur and cur in versions:
                self._mc_combo.setCurrentText(cur)
            elif versions:
                self._mc_combo.setCurrentIndex(0)
        else:
            self._mc_combo.setPlaceholderText("无法获取版本列表，请手动输入版本号")
        self._mc_combo.blockSignals(False)

    # ---------- Java 浏览 ----------
    def _pick_java(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        f, _ = QFileDialog.getOpenFileName(
            self, "选择 java 可执行文件", "", "可执行文件 (*.exe);;所有文件 (*)"
        )
        if f:
            self._java.setText(f)

    # ---------- 提交 ----------
    def _on_create(self) -> None:
        mc = self._mc_combo.currentText().strip()
        if not mc:
            QMessageBox.warning(self, "提示", "请填写 Minecraft 版本")
            return
        loader = self._loader_combo.currentData() or "vanilla"
        if loader != "vanilla" and not self._java.text().strip():
            QMessageBox.warning(
                self, "提示",
                f"安装 {loader.capitalize()} 加载器需要 Java 路径，请填写或先在设置中配置。",
            )
            return
        self.accept()

    # ---------- 取值 ----------
    def values(self) -> dict:
        return {
            "display_name": self._display.text().strip(),
            "game_version": self._mc_combo.currentText().strip(),
            "mod_loader": self._loader_combo.currentData() or "vanilla",
            "mod_loader_version": self._loader_ver.text().strip(),
            "java_path": self._java.text().strip(),
            "isolated": self._isolated.isChecked(),
        }


class EditVersionDialog(QDialog):
    """编辑版本独立配置：显示名、Java、JVM 参数、隔离开关。"""

    def __init__(self, inst, global_java: str = "", global_jvm: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"编辑版本 · {inst.effective_display_name}")
        self.setMinimumWidth(460)
        self._inst = inst

        form = QFormLayout()
        self._display = QLineEdit(inst.display_name)
        self._display.setPlaceholderText(f"留空则用 {inst.version_id}")
        form.addRow("显示名称", self._display)

        # 只读信息
        info = QLabel(
            f"版本 ID：{inst.version_id}\n"
            f"游戏版本：{inst.game_version or '未知'}\n"
            f"加载器：{inst.mod_loader}"
            + (f" {inst.mod_loader_version}" if inst.mod_loader_version else "")
        )
        info.setObjectName("hint")
        form.addRow("", info)

        self._java = QLineEdit(inst.java_path)
        self._java.setPlaceholderText(f"留空则用全局 Java（{global_java or '未配置'}）")
        btn_java = QPushButton("浏览…")
        btn_java.clicked.connect(self._pick_java)
        row_java = QHBoxLayout()
        row_java.addWidget(self._java)
        row_java.addWidget(btn_java)
        java_w = QWidget()
        java_w.setLayout(row_java)
        form.addRow("Java 路径", java_w)

        self._jvm = QLineEdit(" ".join(inst.jvm_args))
        self._jvm.setPlaceholderText(
            f"留空则用全局 JVM 参数（{global_jvm or '自动分配内存+优化'}）"
        )
        form.addRow("JVM 参数", self._jvm)

        self._isolated = QCheckBox("版本隔离（存档/模组/配置独立）")
        self._isolated.setChecked(inst.isolated)
        form.addRow("", self._isolated)

        iso_hint = QLabel(
            "勾选后该版本的存档/模组/配置存在 versions/<id>/ 下，与其它版本互不干扰；\n"
            "不勾选则所有版本共用同一存档目录（HMCL 非隔离模式）。"
        )
        iso_hint.setObjectName("hint")
        iso_hint.setWordWrap(True)
        form.addRow("", iso_hint)

        btn_save = QPushButton("保存")
        btn_save.setObjectName("primary")
        btn_save.setDefault(True)
        btn_save.clicked.connect(self.accept)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        row_btn = QHBoxLayout()
        row_btn.addStretch()
        row_btn.addWidget(btn_cancel)
        row_btn.addWidget(btn_save)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(row_btn)

    def _pick_java(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        f, _ = QFileDialog.getOpenFileName(
            self, "选择 java 可执行文件", "", "可执行文件 (*.exe);;所有文件 (*)"
        )
        if f:
            self._java.setText(f)

    def values(self) -> dict:
        return {
            "display_name": self._display.text().strip(),
            "java_path": self._java.text().strip(),
            "jvm_args": self._jvm.text().split(),
            "isolated": self._isolated.isChecked(),
        }
