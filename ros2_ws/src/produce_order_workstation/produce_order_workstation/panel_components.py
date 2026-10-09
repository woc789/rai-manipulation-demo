"""Lightweight native Qt components; no web runtime or downloaded icon assets."""

from PyQt5.QtCore import Qt, QRectF
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget


NAMES = {"corn": "玉米", "carrot": "胡萝卜", "apple": "苹果", "tomato": "番茄"}
STATE_NAMES = {
    "INITIALIZING": "准备中", "READY": "就绪", "RUNNING": "执行中",
    "RECOVERING": "恢复中", "REPLENISHING": "备货中", "RECOVERY_REQUIRED": "需要恢复",
    "COMPLETED": "配货完成", "FAILED": "失败", "CANCELLED": "已取消",
    "WAITING_PICKUP": "待取货", "COLLECTED": "已取货", "FREE": "空闲",
    "ALLOCATED": "已分配", "SUBMITTING": "提交中", "REJECTED": "未接收",
    "UNKNOWN": "状态未知", "OFFLINE": "离线",
    "PAUSED": "暂停 · 保留箱位", "SCANNING": "视觉复查中",
    "CANCELED": "已取消",
    "INVENTORY_ERROR": "库存复查失败",
    "EXECUTION_UNCERTAIN": "执行结果不明 · 待核对",
}


def label(text="", name="", wrap=False):
    widget = QLabel(text)
    widget.setObjectName(name)
    widget.setWordWrap(wrap)
    widget.setTextFormat(Qt.PlainText)
    return widget


class Card(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(16, 14, 16, 14)
        self.box.setSpacing(10)


class Metric(Card):
    def __init__(self, title, value="—", hint=""):
        super().__init__()
        self.box.setContentsMargins(18, 12, 18, 12)
        self.value = label(value, "metricValue")
        self.box.addWidget(label(title, "muted"))
        self.box.addWidget(self.value)
        self.hint = label(hint, "muted")
        self.box.addWidget(self.hint)
        self.hint.setVisible(bool(hint))


class ProduceGlyph(QWidget):
    def __init__(self, sku):
        super().__init__()
        self.sku = sku
        self.setFixedSize(42, 42)
        self.setAccessibleName(NAMES[sku])

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        colors = {"corn": "#fff3d0", "carrot": "#ffead9", "apple": "#e7f1dd", "tomato": "#ffe5e2"}
        p.setBrush(QColor(colors[self.sku]))
        p.drawRoundedRect(QRectF(0, 0, 42, 42), 12, 12)
        if self.sku == "corn":
            p.setBrush(QColor("#e9b536")); p.drawEllipse(QRectF(15, 8, 12, 26))
            p.setPen(QPen(QColor("#f7d66d"), 1.4))
            for y in (14, 19, 24, 29): p.drawLine(16, y, 25, y)
            p.setPen(Qt.NoPen); p.setBrush(QColor("#69944b"))
            path = QPainterPath(); path.moveTo(21, 35); path.quadTo(8, 27, 11, 18); path.quadTo(21, 23, 21, 35)
            p.drawPath(path)
        elif self.sku == "carrot":
            p.setBrush(QColor("#ed8d38"))
            path = QPainterPath(); path.moveTo(14, 14); path.quadTo(23, 9, 28, 18); path.lineTo(15, 34); path.closeSubpath()
            p.drawPath(path); p.setPen(QPen(QColor("#69944b"), 3))
            p.drawLine(24, 14, 26, 7); p.drawLine(24, 14, 32, 11)
        else:
            p.setBrush(QColor("#8ba960" if self.sku == "apple" else "#df6b59"))
            p.drawEllipse(QRectF(10, 14, 14, 19)); p.drawEllipse(QRectF(19, 14, 14, 19))
            p.setBrush(QColor("#658647")); p.drawEllipse(QRectF(22, 8, 9, 6))
            p.setPen(QPen(QColor("#526849"), 2)); p.drawLine(21, 16, 21, 10)


class BinCard(QFrame):
    def __init__(self, bin_id):
        super().__init__()
        self.setObjectName("binCard")
        self.setProperty("tone", "neutral")
        box = QVBoxLayout(self); box.setContentsMargins(12, 10, 12, 10); box.setSpacing(5)
        row = QHBoxLayout()
        self.title = label(bin_id, "binTitle")
        self.state = label("等待同步", "badge")
        row.addWidget(self.title); row.addStretch(); row.addWidget(self.state)
        box.addLayout(row)
        self.order = label("—", "binOrder")
        box.addWidget(self.order)

    def update_resource(self, resource, online, mode):
        state = resource.get("state", "UNKNOWN")
        text = "空位 · 无箱" if state == "FREE" and mode == "fulfillment" else STATE_NAMES.get(state, state)
        self.state.setText(text if online else "状态未同步")
        order_id = resource.get("order_id")
        self.order.setText("…" + order_id[-18:] if order_id else "接单后放入空箱" if mode == "fulfillment" else "空箱")
        self.order.setToolTip(order_id or "")
        tone = "green" if state == "WAITING_PICKUP" else "amber" if state == "RECOVERY_REQUIRED" else "blue" if state == "ALLOCATED" else "neutral"
        self.setProperty("tone", tone if online else "neutral")
        self.style().unpolish(self); self.style().polish(self)


class StageStrip(QWidget):
    def __init__(self):
        super().__init__()
        row = QHBoxLayout(self); row.setContentsMargins(0, 0, 0, 0); row.setSpacing(8)
        self.steps = []
        for index, name in enumerate(("接单", "定位", "抓取", "装箱", "校验")):
            step = label(f"{index + 1}  {name}", "step")
            step.setAlignment(Qt.AlignCenter)
            row.addWidget(step, 1); self.steps.append(step)
        self.set_stage(-1)

    def set_stage(self, index):
        # Per-item steps may move backwards on the next item; not an order-level percentage.
        for i, step in enumerate(self.steps):
            step.setProperty("active", i == index)
            step.style().unpolish(step); step.style().polish(step)


STYLE = """
QWidget { font-family: 'Noto Sans CJK SC', 'DejaVu Sans'; font-size: 12px; color: #273d3a; }
QWidget#operatorPanel { background: #f3f6f5; }
QFrame#sidebar { background: #173a35; border: none; }
QFrame#sidebar QLabel { color: #a5beb7; }
QFrame#sidebar QLabel#brand { color: white; font-size: 18px; font-weight: 700; }
QFrame#sidebar QLabel#brandTag { color: #d0e3dc; font-size: 11px; }
QFrame#sidebar QPushButton { text-align: left; color: #b9d0c8; background: transparent; border: none; padding: 12px; }
QFrame#sidebar QPushButton:checked { background: #2b5149; color: white; }
QFrame#sidebar QPushButton:hover:enabled { background: #24493f; }
QFrame#sidebar QPushButton:disabled { color: #78948b; }
QFrame#card { background: white; border: 1px solid #e1e9e5; border-radius: 12px; }
QLabel#pageTitle { font-size: 23px; font-weight: 700; }
QLabel#sectionTitle { font-size: 15px; font-weight: 700; }
QLabel#muted, QLabel#binOrder { color: #788b84; font-size: 11px; }
QLabel#metricValue { font-size: 22px; font-weight: 700; color: #244a3f; }
QLabel#hero { font-size: 19px; font-weight: 700; }
QLabel#badge { background: #edf4ef; color: #49725a; border-radius: 6px; padding: 4px 9px; font-size: 11px; }
QLabel#connection { background: #e7f2eb; color: #43785a; border-radius: 14px; padding: 7px 14px; }
QLineEdit, QComboBox { background: #f7f9f8; border: 1px solid #e0e7e3; border-radius: 6px; padding: 6px 9px; min-height: 23px; }
QLineEdit:focus, QComboBox:focus { border: 1px solid #43896b; }
QSpinBox { background: #f7f9f8; border: 1px solid #dfe7e2; border-radius: 6px; padding: 5px 8px; min-height: 26px; font-size: 14px; }
QSpinBox:focus { border: 1px solid #43896b; }
QPushButton { background: white; border: 1px solid #dce5df; border-radius: 7px; padding: 8px 14px; }
QPushButton:hover:enabled { background: #eef5f0; border-color: #a9c6b4; }
QPushButton#primary { background: #377a5c; color: white; border: none; font-weight: 600; padding: 12px; }
QPushButton#primary:hover:enabled { background: #2d684e; }
QPushButton#danger { color: #a66945; background: #fbf7f2; border-color: #ede0d0; }
QPushButton:disabled { color: #aab7af; background: #f0f3f1; border-color: #e8eeea; }
QPushButton#primary:disabled { color: #a1b4a8; background: #e6eee8; }
QFrame#binCard { background: #f8faf9; border: 1px solid #e2eae5; border-radius: 8px; }
QFrame#binCard[tone='green'] { background: #eef7ef; border-color: #b8d4bd; }
QFrame#binCard[tone='blue'] { background: #eef4f9; border-color: #bed2e2; }
QFrame#binCard[tone='amber'] { background: #fff7e8; border-color: #e4cda6; }
QLabel#binTitle { font-size: 15px; font-weight: 700; }
QLabel#step { background: #f0f4f2; border-radius: 6px; color: #82958a; padding: 8px 3px; font-size: 11px; }
QLabel#step[active='true'] { background: #dceee1; color: #377456; font-weight: 600; }
QProgressBar { background: #edf2ef; border: none; border-radius: 4px; min-height: 8px; max-height: 8px; }
QProgressBar::chunk { background: #5d9c76; border-radius: 4px; }
QTabWidget::pane { border: none; background: white; }
QTabBar::tab { color: #7c8f83; padding: 8px 13px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #397453; border-bottom: 2px solid #397453; }
QTableWidget { background: white; border: none; gridline-color: #edf2ee; selection-background-color: #edf5ef; selection-color: #274d36; }
QHeaderView::section { background: #f7f9f7; color: #7f9185; border: none; border-bottom: 1px solid #e6ece8; padding: 7px; font-size: 11px; }
QPlainTextEdit { background: #f8faf9; border: none; padding: 8px; selection-background-color: #cbded0; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 7px; }
QScrollBar::handle:vertical { background: #ccd8d0; border-radius: 3px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""
