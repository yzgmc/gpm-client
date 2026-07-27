"""主题系统：黑色系 / 黑白系 两套配色，支持运行时切换。

设计原则：
- 所有颜色集中在 _DarkPalette / _LightPalette 中，QSS 模板只引用变量。
- 顶部菜单栏与主窗口保持同一色系，杜绝白/黑割裂感。
- 切换主题只需调用 apply_theme(app, "dark"|"light")，立即生效。
- 主题持久化在 ClientConfig.theme，重启后恢复。

配色规范（dark）：
- 主窗口背景：#0D0E12
- 卡片/容器：#16181D
- 悬浮态：#22252E
- 边框：#2A2E39
- 主题色：#FF9500 (Accent)
- 文字：主标题 #FFFFFF / 正文 #D1D5DB / 次要 #6B7280

配色规范（light 黑白）：
- 主窗口背景：#FAFAFA
- 卡片/容器：#FFFFFF
- 悬浮态：#F0F0F0
- 边框：#D4D4D4
- 主题色：#1A1A1A (深灰，纯黑白模式)
- 文字：主标题 #0A0A0A / 正文 #2B2B2B / 次要 #6E6E6E
"""

from __future__ import annotations

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication


# ============ 暗色色板 ============
class _DarkPalette:
    BG = "#0D0E12"               # 主窗口背景
    CARD = "#16181D"             # 卡片/容器
    HOVER = "#22252E"            # 悬浮态
    BORDER = "#2A2E39"           # 边框
    ACCENT = "#FF9500"           # 主题色（琥珀金）
    ACCENT_HOVER = "#FFB340"     # 主题色悬浮
    ACCENT_PRESSED = "#E08600"   # 主题色按下
    ACCENT_FG = "#1A1000"        # 主题色上的前景文字
    TEXT_PRIMARY = "#FFFFFF"     # 主标题
    TEXT_BODY = "#D1D5DB"        # 正文
    TEXT_MUTED = "#6B7280"       # 次要文字
    HEADER_BG = "#1A1C23"        # 表头背景
    SCROLL_HANDLE = "#3A3F4B"    # 滚动条手柄
    SCROLL_HANDLE_HOVER = "#4A5060"
    SELECTION_BG = "rgba(255, 149, 0, 0.18)"  # 选中背景
    NAV_SELECTED_BG = "rgba(255, 149, 0, 0.10)"
    BTN_DISABLED_BG = "#121317"
    SUCCESS = "#30D158"
    ERROR = "#FF453A"
    WARNING = "#FF9F0A"
    INFO = "#0A84FF"


# ============ 亮色色板（黑白主题）============
class _LightPalette:
    BG = "#FAFAFA"               # 主窗口背景（柔和白）
    CARD = "#FFFFFF"             # 卡片/容器（纯白）
    HOVER = "#F0F0F0"            # 悬浮态（极浅灰）
    BORDER = "#D4D4D4"           # 边框（中浅灰）
    ACCENT = "#1A1A1A"           # 主题色（深黑，纯黑白模式）
    ACCENT_HOVER = "#3A3A3A"     # 悬浮态（中黑）
    ACCENT_PRESSED = "#000000"   # 按下态（纯黑）
    ACCENT_FG = "#FFFFFF"        # 主题色上的前景文字（白）
    TEXT_PRIMARY = "#0A0A0A"     # 主标题（近黑）
    TEXT_BODY = "#2B2B2B"        # 正文（深灰）
    TEXT_MUTED = "#6E6E6E"       # 次要文字（中灰）
    HEADER_BG = "#F5F5F5"        # 表头背景（极浅灰）
    SCROLL_HANDLE = "#BDBDBD"    # 滚动条手柄
    SCROLL_HANDLE_HOVER = "#9A9A9A"
    SELECTION_BG = "rgba(0, 0, 0, 0.08)"  # 选中背景（灰阶）
    NAV_SELECTED_BG = "rgba(0, 0, 0, 0.06)"
    BTN_DISABLED_BG = "#F5F5F5"
    SUCCESS = "#0F7A2E"
    ERROR = "#C0322A"
    WARNING = "#A86A00"
    INFO = "#0066CC"


# ============ 主题注册表 ============
THEMES: dict[str, type] = {
    "dark": _DarkPalette,
    "light": _LightPalette,
}
DEFAULT_THEME = "dark"


def build_stylesheet(theme: str) -> str:
    """根据主题名生成完整 QSS 样式表。

    关键设计：
    - QMenuBar 与 QMainWindow 共享同一背景色，杜绝"白顶 + 黑底"割裂感。
    - 所有颜色来自 palette，无硬编码。
    """
    p = THEMES.get(theme, _DarkPalette)
    return f"""
/* ===== 全局基础 ===== */
QWidget {{
    background-color: {p.BG};
    color: {p.TEXT_BODY};
    font-family: 'Microsoft YaHei', 'Segoe UI', 'PingFang SC', 'SF Pro', sans-serif;
    font-size: 13px;
}}

/* 主窗口、对话框与菜单栏同色——消除顶/底割裂感 */
QMainWindow,
QDialog,
QMenuBar {{
    background-color: {p.BG};
    color: {p.TEXT_BODY};
}}

/* ===== 滚动条 ===== */
QScrollBar:vertical {{
    background: {p.BG};
    width: 10px;
    margin: 0;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {p.SCROLL_HANDLE};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {p.SCROLL_HANDLE_HOVER};
}}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {{
    height: 0;
    background: none;
}}
QScrollBar:horizontal {{
    background: {p.BG};
    height: 10px;
    margin: 0;
    border: none;
}}
QScrollBar::handle:horizontal {{
    background: {p.SCROLL_HANDLE};
    border-radius: 5px;
    min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {p.SCROLL_HANDLE_HOVER};
}}
QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {{
    width: 0;
    background: none;
}}

/* ===== QLabel ===== */
QLabel {{
    background: transparent;
    color: {p.TEXT_BODY};
    border: none;
}}
QLabel#title {{
    color: {p.TEXT_PRIMARY};
    font-size: 16px;
    font-weight: 600;
}}
QLabel#subtitle {{
    color: {p.TEXT_PRIMARY};
    font-size: 14px;
    font-weight: 600;
}}
QLabel#hint {{
    color: {p.TEXT_MUTED};
    font-size: 11px;
}}
QLabel#count {{
    color: {p.TEXT_MUTED};
    font-size: 12px;
}}
QLabel#error {{
    color: {p.ERROR};
    font-size: 12px;
}}
QLabel#success {{
    color: {p.SUCCESS};
    font-size: 12px;
}}

/* ===== QPushButton（普通按钮）===== */
QPushButton {{
    background-color: {p.CARD};
    color: {p.TEXT_BODY};
    border: 1px solid {p.BORDER};
    border-radius: 6px;
    padding: 8px 14px;
    font-size: 13px;
}}
QPushButton:hover {{
    background-color: {p.HOVER};
    border-color: {p.SCROLL_HANDLE};
}}
QPushButton:pressed {{
    background-color: {p.HEADER_BG};
}}
QPushButton:disabled {{
    color: {p.TEXT_MUTED};
    background-color: {p.BTN_DISABLED_BG};
    border-color: {p.BORDER};
}}
QPushButton:disabled:hover {{
    background-color: {p.BTN_DISABLED_BG};
}}

/* ===== QPushButton（主按钮 / 主题色）===== */
QPushButton#primary {{
    background-color: {p.ACCENT};
    color: {p.ACCENT_FG};
    border: none;
    border-radius: 6px;
    padding: 9px 16px;
    font-size: 13px;
    font-weight: 600;
}}
QPushButton#primary:hover {{
    background-color: {p.ACCENT_HOVER};
}}
QPushButton#primary:pressed {{
    background-color: {p.ACCENT_PRESSED};
}}
QPushButton#primary:disabled {{
    background-color: {p.HOVER};
    color: {p.TEXT_MUTED};
}}

/* ===== QPushButton（危险按钮）===== */
QPushButton#danger {{
    background-color: {p.CARD};
    color: {p.ERROR};
    border: 1px solid {p.ERROR};
    border-radius: 6px;
    padding: 8px 14px;
}}
QPushButton#danger:hover {{
    background-color: {p.HOVER};
    border-color: {p.ERROR};
}}
QPushButton#danger:pressed {{
    background-color: {p.HOVER};
}}

/* ===== QLineEdit / QTextEdit / QPlainTextEdit ===== */
QLineEdit,
QTextEdit,
QPlainTextEdit {{
    background-color: {p.CARD};
    color: {p.TEXT_PRIMARY};
    border: 1px solid {p.BORDER};
    border-radius: 6px;
    padding: 8px 12px;
    selection-background-color: {p.ACCENT};
    selection-color: {p.ACCENT_FG};
}}
QLineEdit:focus,
QTextEdit:focus,
QPlainTextEdit:focus {{
    border: 1px solid {p.ACCENT};
}}
QLineEdit:disabled {{
    color: {p.TEXT_MUTED};
    background-color: {p.BTN_DISABLED_BG};
}}
QLineEdit[readOnly="true"] {{
    background-color: {p.BTN_DISABLED_BG};
    color: {p.TEXT_MUTED};
}}

/* ===== QComboBox ===== */
QComboBox {{
    background-color: {p.CARD};
    color: {p.TEXT_PRIMARY};
    border: 1px solid {p.BORDER};
    border-radius: 6px;
    padding: 7px 12px;
    min-height: 18px;
}}
QComboBox:hover {{
    border-color: {p.SCROLL_HANDLE};
}}
QComboBox:focus {{
    border: 1px solid {p.ACCENT};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {p.TEXT_MUTED};
    margin-right: 8px;
}}
QComboBox::down-arrow:on {{
    border-top: 5px solid {p.ACCENT};
}}
QComboBox QAbstractItemView {{
    background-color: {p.CARD};
    color: {p.TEXT_PRIMARY};
    border: 1px solid {p.BORDER};
    border-radius: 6px;
    padding: 4px;
    outline: none;
    selection-background-color: {p.ACCENT};
    selection-color: {p.ACCENT_FG};
}}
QComboBox QAbstractItemView::item {{
    padding: 6px 10px;
    min-height: 22px;
    border-radius: 4px;
}}
QComboBox QAbstractItemView::item:hover {{
    background-color: {p.HOVER};
}}

/* ===== QCheckBox / QRadioButton ===== */
QCheckBox,
QRadioButton {{
    background: transparent;
    color: {p.TEXT_BODY};
    spacing: 8px;
    padding: 4px 0;
}}
QCheckBox::indicator,
QRadioButton::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {p.SCROLL_HANDLE};
    background: {p.CARD};
}}
QCheckBox::indicator {{
    border-radius: 3px;
}}
QRadioButton::indicator {{
    border-radius: 9px;
}}
QCheckBox::indicator:hover,
QRadioButton::indicator:hover {{
    border-color: {p.ACCENT};
}}
QCheckBox::indicator:checked,
QRadioButton::indicator:checked {{
    background-color: {p.ACCENT};
    border-color: {p.ACCENT};
}}

/* ===== QTableWidget / QTableView ===== */
QTableWidget,
QTableView {{
    background-color: {p.CARD};
    alternate-background-color: {p.HEADER_BG};
    color: {p.TEXT_BODY};
    border: 1px solid {p.BORDER};
    border-radius: 6px;
    gridline-color: {p.BORDER};
    outline: none;
    selection-background-color: {p.SELECTION_BG};
    selection-color: {p.TEXT_PRIMARY};
}}
QTableWidget::item,
QTableView::item {{
    padding: 6px 8px;
    border: none;
    outline: none;
}}
QTableWidget::item:selected,
QTableView::item:selected {{
    background-color: {p.SELECTION_BG};
}}
QTableWidget::item:hover,
QTableView::item:hover {{
    background-color: {p.HOVER};
}}

/* 表头 */
QHeaderView::section {{
    background-color: {p.HEADER_BG};
    color: {p.TEXT_PRIMARY};
    padding: 8px 10px;
    border: none;
    border-right: 1px solid {p.BORDER};
    border-bottom: 1px solid {p.BORDER};
    font-weight: 600;
    text-align: left;
}}
QHeaderView::section:hover {{
    background-color: {p.HOVER};
}}

/* ===== QListWidget ===== */
QListWidget {{
    background-color: {p.CARD};
    alternate-background-color: {p.HEADER_BG};
    color: {p.TEXT_BODY};
    border: 1px solid {p.BORDER};
    border-radius: 6px;
    outline: none;
    padding: 4px;
    selection-background-color: {p.SELECTION_BG};
    selection-color: {p.TEXT_PRIMARY};
}}
QListWidget::item {{
    padding: 8px 10px;
    border-radius: 4px;
    border: none;
    outline: none;
}}
QListWidget::item:hover {{
    background-color: {p.HOVER};
}}
QListWidget::item:selected {{
    background-color: {p.SELECTION_BG};
    color: {p.TEXT_PRIMARY};
}}

/* ===== QProgressBar ===== */
QProgressBar {{
    background-color: {p.BTN_DISABLED_BG};
    color: {p.TEXT_PRIMARY};
    border: 1px solid {p.BORDER};
    border-radius: 6px;
    text-align: center;
    font-size: 11px;
    min-height: 18px;
}}
QProgressBar::chunk {{
    background-color: {p.ACCENT};
    border-radius: 5px;
}}

/* ===== QTabWidget（主 Tab）===== */
QTabWidget::pane {{
    border: none;
    background: {p.BG};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {p.TEXT_MUTED};
    padding: 10px 18px;
    border: none;
    border-bottom: 2px solid transparent;
    font-size: 13px;
    min-width: 80px;
}}
QTabBar::tab:hover {{
    color: {p.TEXT_BODY};
    background: {p.HOVER};
}}
QTabBar::tab:selected {{
    color: {p.TEXT_PRIMARY};
    border-bottom: 2px solid {p.ACCENT};
    font-weight: 600;
}}

/* ===== 侧边导航栏 QListWidget（objectName=navList）===== */
QListWidget#navList {{
    background-color: {p.BG};
    border: none;
    border-right: 1px solid {p.BORDER};
    border-radius: 0;
    padding: 8px 6px;
    outline: none;
    font-size: 13px;
}}
QListWidget#navList::item {{
    padding: 12px 14px;
    border-radius: 6px;
    border-left: 3px solid transparent;
    margin: 2px 0;
}}
QListWidget#navList::item:hover {{
    background-color: {p.HOVER};
    border-left: 3px solid {p.SCROLL_HANDLE};
}}
QListWidget#navList::item:selected {{
    background-color: {p.NAV_SELECTED_BG};
    color: {p.ACCENT};
    border-left: 3px solid {p.ACCENT};
    font-weight: 600;
}}

/* ===== QSplitter ===== */
QSplitter::handle {{
    background-color: {p.BORDER};
}}
QSplitter::handle:horizontal {{
    width: 1px;
}}
QSplitter::handle:vertical {{
    height: 1px;
}}

/* ===== QMenuBar / QMenu（顶/底同色关键）===== */
QMenuBar {{
    background-color: {p.BG};
    color: {p.TEXT_BODY};
    border-bottom: 1px solid {p.BORDER};
    padding: 2px;
}}
QMenuBar::item {{
    background: transparent;
    padding: 6px 12px;
    border-radius: 4px;
}}
QMenuBar::item:hover {{
    background-color: {p.HOVER};
}}
QMenuBar::item:pressed {{
    background-color: {p.HEADER_BG};
}}
QMenu {{
    background-color: {p.CARD};
    color: {p.TEXT_BODY};
    border: 1px solid {p.BORDER};
    border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{
    padding: 7px 24px 7px 14px;
    border-radius: 4px;
}}
QMenu::item:hover {{
    background-color: {p.HOVER};
}}
QMenu::item:disabled {{
    color: {p.TEXT_MUTED};
}}
QMenu::separator {{
    height: 1px;
    background: {p.BORDER};
    margin: 4px 8px;
}}

/* ===== QStatusBar ===== */
QStatusBar {{
    background-color: {p.BG};
    color: {p.TEXT_MUTED};
    border-top: 1px solid {p.BORDER};
    padding: 2px 8px;
    font-size: 11px;
}}
QStatusBar::item {{
    border: none;
}}

/* ===== QGroupBox / QFrame（卡片容器）===== */
QGroupBox,
QFrame#card {{
    background-color: {p.CARD};
    border: 1px solid {p.BORDER};
    border-radius: 8px;
    padding: 16px;
    margin-top: 8px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 8px;
    color: {p.TEXT_PRIMARY};
    font-size: 14px;
    font-weight: 600;
    background-color: {p.BG};
}}

/* ===== QMessageBox ===== */
QMessageBox {{
    background-color: {p.CARD};
}}
QMessageBox QLabel {{
    color: {p.TEXT_BODY};
    min-width: 280px;
}}
QMessageBox QPushButton {{
    min-width: 70px;
    padding: 7px 16px;
}}

/* ===== QToolTip ===== */
QToolTip {{
    background-color: {p.CARD};
    color: {p.TEXT_PRIMARY};
    border: 1px solid {p.BORDER};
    border-radius: 4px;
    padding: 6px 10px;
    font-size: 12px;
}}

/* ===== QProgressDialog ===== */
QProgressDialog {{
    background-color: {p.CARD};
}}
QProgressDialog QProgressBar {{
    min-height: 20px;
}}
"""


# 保留旧名向后兼容：默认暗色样式表
DARK_STYLE_SHEET = build_stylesheet("dark")
LIGHT_STYLE_SHEET = build_stylesheet("light")


def apply_theme(app: QApplication, theme: str) -> None:
    """对 QApplication 应用指定主题（dark / light）。

    - 立即生效：QApplication.setStyleSheet 会刷新所有已注册 QWidget。
    - 字体也按主题微调（深色用正常字重，亮色用稍细字重以提升可读性）。
    """
    if theme not in THEMES:
        theme = DEFAULT_THEME

    # 字体：深色偏好稍粗字重，亮色偏好细字重（白底黑字过粗易显笨重）
    font = QFont("Microsoft YaHei", 10)
    font.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(font)

    # 应用主题样式表
    app.setStyleSheet(build_stylesheet(theme))


def apply_dark_theme(app: QApplication) -> None:
    """向后兼容：等价于 apply_theme(app, "dark")。"""
    apply_theme(app, "dark")


# ============ objectName 常量（代码中 setObjectName 引用，保持一致）============
# Label 角色
OBJ_TITLE = "title"
OBJ_SUBTITLE = "subtitle"
OBJ_HINT = "hint"
OBJ_COUNT = "count"
OBJ_ERROR = "error"
OBJ_SUCCESS = "success"

# 按钮角色
OBJ_PRIMARY = "primary"
OBJ_DANGER = "danger"

# 容器
OBJ_CARD = "card"
OBJ_NAV_LIST = "navList"
