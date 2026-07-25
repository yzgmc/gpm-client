"""黑色系高质感 QSS 主题。

提供全局 DARK_STYLE_SHEET 与应用主题的便捷函数。
配色：纯深黑底 + 暗灰卡片 + 琥珀金主题色（#FF9500）。

设计规范：
- 主窗口背景：#0D0E12
- 卡片/容器：#16181D
- 悬浮态：#22252E
- 边框：#2A2E39 (1px)
- 主题色：#FF9500 (Accent)
- 文字：主标题 #FFFFFF / 正文 #D1D5DB / 次要 #6B7280
- 统一圆角 6~8px，内边距 8px 12px
"""

from __future__ import annotations

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication


# ============ 色板常量（供代码中 setStyleSheet 引用，保持一致）============
class Palette:
    BG = "#0D0E12"               # 主窗口背景
    CARD = "#16181D"             # 卡片/容器
    HOVER = "#22252E"            # 悬浮态
    BORDER = "#2A2E39"           # 边框
    ACCENT = "#FF9500"           # 主题色（琥珀金）
    ACCENT_HOVER = "#FFB340"     # 主题色悬浮
    ACCENT_PRESSED = "#E08600"   # 主题色按下
    TEXT_PRIMARY = "#FFFFFF"     # 主标题
    TEXT_BODY = "#D1D5DB"        # 正文
    TEXT_MUTED = "#6B7280"       # 次要文字
    HEADER_BG = "#1A1C23"        # 表头背景
    SUCCESS = "#30D158"          # 成功
    ERROR = "#FF453A"            # 错误
    WARNING = "#FF9F0A"          # 警告
    INFO = "#0A84FF"             # 信息


# ============ 全局 QSS ============
DARK_STYLE_SHEET = f"""
/* ===== 全局基础 ===== */
QWidget {{
    background-color: {Palette.BG};
    color: {Palette.TEXT_BODY};
    font-family: 'Microsoft YaHei', 'Segoe UI', 'PingFang SC', 'SF Pro', sans-serif;
    font-size: 13px;
}}

QMainWindow,
QDialog {{
    background-color: {Palette.BG};
}}

/* ===== 滚动条 ===== */
QScrollBar:vertical {{
    background: {Palette.BG};
    width: 10px;
    margin: 0;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: #3A3F4B;
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: #4A5060;
}}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {{
    height: 0;
    background: none;
}}
QScrollBar:horizontal {{
    background: {Palette.BG};
    height: 10px;
    margin: 0;
    border: none;
}}
QScrollBar::handle:horizontal {{
    background: #3A3F4B;
    border-radius: 5px;
    min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{
    background: #4A5060;
}}
QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {{
    width: 0;
    background: none;
}}

/* ===== QLabel ===== */
QLabel {{
    background: transparent;
    color: {Palette.TEXT_BODY};
    border: none;
}}
QLabel#title {{
    color: {Palette.TEXT_PRIMARY};
    font-size: 16px;
    font-weight: 600;
}}
QLabel#subtitle {{
    color: {Palette.TEXT_PRIMARY};
    font-size: 14px;
    font-weight: 600;
}}
QLabel#hint {{
    color: {Palette.TEXT_MUTED};
    font-size: 11px;
}}
QLabel#count {{
    color: {Palette.TEXT_MUTED};
    font-size: 12px;
}}
QLabel#error {{
    color: {Palette.ERROR};
    font-size: 12px;
}}
QLabel#success {{
    color: {Palette.SUCCESS};
    font-size: 12px;
}}

/* ===== QPushButton（普通按钮）===== */
QPushButton {{
    background-color: {Palette.CARD};
    color: {Palette.TEXT_BODY};
    border: 1px solid {Palette.BORDER};
    border-radius: 6px;
    padding: 8px 14px;
    font-size: 13px;
}}
QPushButton:hover {{
    background-color: {Palette.HOVER};
    border-color: #3A3F4B;
}}
QPushButton:pressed {{
    background-color: #1A1C23;
}}
QPushButton:disabled {{
    color: {Palette.TEXT_MUTED};
    background-color: #121317;
    border-color: #1F222B;
}}
QPushButton:disabled:hover {{
    background-color: #121317;
}}

/* ===== QPushButton（主按钮 / 主题色）===== */
QPushButton#primary {{
    background-color: {Palette.ACCENT};
    color: #1A1000;
    border: none;
    border-radius: 6px;
    padding: 9px 16px;
    font-size: 13px;
    font-weight: 600;
}}
QPushButton#primary:hover {{
    background-color: {Palette.ACCENT_HOVER};
}}
QPushButton#primary:pressed {{
    background-color: {Palette.ACCENT_PRESSED};
}}
QPushButton#primary:disabled {{
    background-color: #4A3815;
    color: #8A6B3A;
}}

/* ===== QPushButton（危险按钮）===== */
QPushButton#danger {{
    background-color: {Palette.CARD};
    color: {Palette.ERROR};
    border: 1px solid #4A2018;
    border-radius: 6px;
    padding: 8px 14px;
}}
QPushButton#danger:hover {{
    background-color: #2A1410;
    border-color: {Palette.ERROR};
}}
QPushButton#danger:pressed {{
    background-color: #1F0F0C;
}}

/* ===== QLineEdit / QTextEdit / QPlainTextEdit ===== */
QLineEdit,
QTextEdit,
QPlainTextEdit {{
    background-color: {Palette.CARD};
    color: {Palette.TEXT_PRIMARY};
    border: 1px solid {Palette.BORDER};
    border-radius: 6px;
    padding: 8px 12px;
    selection-background-color: {Palette.ACCENT};
    selection-color: #1A1000;
}}
QLineEdit:focus,
QTextEdit:focus,
QPlainTextEdit:focus {{
    border: 1px solid {Palette.ACCENT};
}}
QLineEdit:disabled {{
    color: {Palette.TEXT_MUTED};
    background-color: #121317;
}}
QLineEdit[readOnly="true"] {{
    background-color: #121317;
    color: {Palette.TEXT_MUTED};
}}

/* ===== QComboBox ===== */
QComboBox {{
    background-color: {Palette.CARD};
    color: {Palette.TEXT_PRIMARY};
    border: 1px solid {Palette.BORDER};
    border-radius: 6px;
    padding: 7px 12px;
    min-height: 18px;
}}
QComboBox:hover {{
    border-color: #3A3F4B;
}}
QComboBox:focus {{
    border: 1px solid {Palette.ACCENT};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {Palette.TEXT_MUTED};
    margin-right: 8px;
}}
QComboBox::down-arrow:on {{
    border-top: 5px solid {Palette.ACCENT};
}}
QComboBox QAbstractItemView {{
    background-color: {Palette.CARD};
    color: {Palette.TEXT_PRIMARY};
    border: 1px solid {Palette.BORDER};
    border-radius: 6px;
    padding: 4px;
    outline: none;
    selection-background-color: {Palette.ACCENT};
    selection-color: #1A1000;
}}
QComboBox QAbstractItemView::item {{
    padding: 6px 10px;
    min-height: 22px;
    border-radius: 4px;
}}
QComboBox QAbstractItemView::item:hover {{
    background-color: {Palette.HOVER};
}}

/* ===== QCheckBox / QRadioButton ===== */
QCheckBox,
QRadioButton {{
    background: transparent;
    color: {Palette.TEXT_BODY};
    spacing: 8px;
    padding: 4px 0;
}}
QCheckBox::indicator,
QRadioButton::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid #4A5060;
    background: {Palette.CARD};
}}
QCheckBox::indicator {{
    border-radius: 3px;
}}
QRadioButton::indicator {{
    border-radius: 9px;
}}
QCheckBox::indicator:hover,
QRadioButton::indicator:hover {{
    border-color: {Palette.ACCENT};
}}
QCheckBox::indicator:checked,
QRadioButton::indicator:checked {{
    background-color: {Palette.ACCENT};
    border-color: {Palette.ACCENT};
}}
QCheckBox::indicator:checked {{
    image: none;
    /* 用一个白色对勾近似（QSS 无 svg 内嵌，靠 border 模拟）*/
}}

/* ===== QTableWidget / QTableView ===== */
QTableWidget,
QTableView {{
    background-color: {Palette.CARD};
    alternate-background-color: #1A1C23;
    color: {Palette.TEXT_BODY};
    border: 1px solid {Palette.BORDER};
    border-radius: 6px;
    gridline-color: #1F222B;
    outline: none;
    selection-background-color: rgba(255, 149, 0, 0.18);
    selection-color: {Palette.TEXT_PRIMARY};
}}
QTableWidget::item,
QTableView::item {{
    padding: 6px 8px;
    border: none;
    outline: none;
}}
QTableWidget::item:selected,
QTableView::item:selected {{
    background-color: rgba(255, 149, 0, 0.18);
}}
QTableWidget::item:hover,
QTableView::item:hover {{
    background-color: {Palette.HOVER};
}}

/* 表头 */
QHeaderView::section {{
    background-color: {Palette.HEADER_BG};
    color: {Palette.TEXT_PRIMARY};
    padding: 8px 10px;
    border: none;
    border-right: 1px solid {Palette.BORDER};
    border-bottom: 1px solid {Palette.BORDER};
    font-weight: 600;
    text-align: left;
}}
QHeaderView::section:hover {{
    background-color: {Palette.HOVER};
}}

/* ===== QListWidget ===== */
QListWidget {{
    background-color: {Palette.CARD};
    alternate-background-color: #1A1C23;
    color: {Palette.TEXT_BODY};
    border: 1px solid {Palette.BORDER};
    border-radius: 6px;
    outline: none;
    padding: 4px;
    selection-background-color: rgba(255, 149, 0, 0.18);
    selection-color: {Palette.TEXT_PRIMARY};
}}
QListWidget::item {{
    padding: 8px 10px;
    border-radius: 4px;
    border: none;
    outline: none;
}}
QListWidget::item:hover {{
    background-color: {Palette.HOVER};
}}
QListWidget::item:selected {{
    background-color: rgba(255, 149, 0, 0.18);
    color: {Palette.TEXT_PRIMARY};
}}

/* ===== QProgressBar ===== */
QProgressBar {{
    background-color: #121317;
    color: {Palette.TEXT_PRIMARY};
    border: 1px solid {Palette.BORDER};
    border-radius: 6px;
    text-align: center;
    font-size: 11px;
    min-height: 18px;
}}
QProgressBar::chunk {{
    background-color: {Palette.ACCENT};
    border-radius: 5px;
}}

/* ===== QTabWidget（主 Tab）===== */
QTabWidget::pane {{
    border: none;
    background: {Palette.BG};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {Palette.TEXT_MUTED};
    padding: 10px 18px;
    border: none;
    border-bottom: 2px solid transparent;
    font-size: 13px;
    min-width: 80px;
}}
QTabBar::tab:hover {{
    color: {Palette.TEXT_BODY};
    background: rgba(255, 255, 255, 0.03);
}}
QTabBar::tab:selected {{
    color: {Palette.TEXT_PRIMARY};
    border-bottom: 2px solid {Palette.ACCENT};
    font-weight: 600;
}}

/* ===== 侧边导航栏 QListWidget（objectName=navList）===== */
QListWidget#navList {{
    background-color: {Palette.BG};
    border: none;
    border-right: 1px solid {Palette.BORDER};
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
    background-color: {Palette.HOVER};
    border-left: 3px solid #4A5060;
}}
QListWidget#navList::item:selected {{
    background-color: rgba(255, 149, 0, 0.10);
    color: {Palette.ACCENT};
    border-left: 3px solid {Palette.ACCENT};
    font-weight: 600;
}}

/* ===== QSplitter ===== */
QSplitter::handle {{
    background-color: {Palette.BORDER};
}}
QSplitter::handle:horizontal {{
    width: 1px;
}}
QSplitter::handle:vertical {{
    height: 1px;
}}

/* ===== QMenuBar / QMenu ===== */
QMenuBar {{
    background-color: {Palette.BG};
    color: {Palette.TEXT_BODY};
    border-bottom: 1px solid {Palette.BORDER};
    padding: 2px;
}}
QMenuBar::item {{
    background: transparent;
    padding: 6px 12px;
    border-radius: 4px;
}}
QMenuBar::item:hover {{
    background-color: {Palette.HOVER};
}}
QMenuBar::item:pressed {{
    background-color: #1A1C23;
}}
QMenu {{
    background-color: {Palette.CARD};
    color: {Palette.TEXT_BODY};
    border: 1px solid {Palette.BORDER};
    border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{
    padding: 7px 24px 7px 14px;
    border-radius: 4px;
}}
QMenu::item:hover {{
    background-color: {Palette.HOVER};
}}
QMenu::item:disabled {{
    color: {Palette.TEXT_MUTED};
}}
QMenu::separator {{
    height: 1px;
    background: {Palette.BORDER};
    margin: 4px 8px;
}}

/* ===== QStatusBar ===== */
QStatusBar {{
    background-color: {Palette.BG};
    color: {Palette.TEXT_MUTED};
    border-top: 1px solid {Palette.BORDER};
    padding: 2px 8px;
    font-size: 11px;
}}
QStatusBar::item {{
    border: none;
}}

/* ===== QGroupBox / QFrame（卡片容器）===== */
QGroupBox,
QFrame#card {{
    background-color: {Palette.CARD};
    border: 1px solid {Palette.BORDER};
    border-radius: 8px;
    padding: 16px;
    margin-top: 8px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 8px;
    color: {Palette.TEXT_PRIMARY};
    font-size: 14px;
    font-weight: 600;
    background-color: {Palette.BG};
}}

/* ===== QMessageBox ===== */
QMessageBox {{
    background-color: {Palette.CARD};
}}
QMessageBox QLabel {{
    color: {Palette.TEXT_BODY};
    min-width: 280px;
}}
QMessageBox QPushButton {{
    min-width: 70px;
    padding: 7px 16px;
}}

/* ===== QToolTip ===== */
QToolTip {{
    background-color: {Palette.CARD};
    color: {Palette.TEXT_PRIMARY};
    border: 1px solid {Palette.BORDER};
    border-radius: 4px;
    padding: 6px 10px;
    font-size: 12px;
}}

/* ===== QProgressDialog ===== */
QProgressDialog {{
    background-color: {Palette.CARD};
}}
QProgressDialog QProgressBar {{
    min-height: 20px;
}}
"""


def apply_dark_theme(app: QApplication) -> None:
    """对 QApplication 应用黑色系主题（QSS + 字体）。"""
    # 全局字体（Windows 首选微软雅黑，macOS 回退苹方）
    font = QFont("Microsoft YaHei", 10)
    font.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(font)
    # 应用全局 QSS
    app.setStyleSheet(DARK_STYLE_SHEET)


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
