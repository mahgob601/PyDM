"""Colors, icons and the Qt stylesheet."""
from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPen,
                           QPixmap)
from PySide6.QtSvg import QSvgRenderer

C = {
    "bg": "#0b0d12", "panel": "#0f1219", "card": "#141821", "card_hover": "#181d28",
    "raised": "#1b2030", "border": "#222837", "border2": "#2c3346", "text": "#e9ecf3",
    "muted": "#8b94a9", "faint": "#596178", "accent": "#7c6cff", "accent2": "#43c6f0",
    "success": "#22c55e", "warn": "#f5a524", "danger": "#f43f5e", "info": "#38bdf8",
}
CAT_COLORS = {"Video": "#f43f5e", "Music": "#a855f7", "Compressed": "#f59e0b",
              "Documents": "#3b82f6", "Programs": "#10b981", "Other": "#7b8499"}
CAT_ICONS = {"Video": "film", "Music": "music", "Compressed": "archive",
             "Documents": "file-text", "Programs": "package", "Other": "file"}
STATUS_STYLE = {   # text color, chip background alpha
    "Downloading": C["accent2"], "Connecting": C["accent2"], "Verifying": C["info"],
    "Extracting": C["info"], "Retrying": C["warn"], "Merging": C["info"],
    "Queued": C["muted"], "Paused": C["warn"], "Completed": C["success"], "Error": C["danger"],
}
LEVEL_COLORS = {"safe": C["success"], "info": C["info"], "caution": C["warn"],
                "danger": C["danger"]}

_S = 'fill="none" stroke="{c}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"'
ICONS = {
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "play": '<path d="M8 5.5v13l10.5-6.5z"/>',
    "pause": '<path d="M9 5v14M15 5v14"/>',
    "trash": '<path d="M4 7h16M10 11v6M14 11v6M9 7V4.5h6V7M6 7l1 13h10l1-13"/>',
    "folder": '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.8l2 2h7.2a2 2 0 0 1 2 2v7.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 2.8v2.4M12 18.8v2.4M4.2 7.5l2.1 1.2M17.7 15.3l2.1 1.2M4.2 16.5l2.1-1.2M17.7 8.7l2.1-1.2"/><circle cx="12" cy="12" r="7"/>',
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/>',
    "download": '<path d="M12 4v11M7.5 10.5L12 15l4.5-4.5M5 19.5h14"/>',
    "layers": '<path d="M12 3.5l8.5 4.5-8.5 4.5L3.5 8z"/><path d="M3.5 12.5l8.5 4.5 8.5-4.5"/><path d="M3.5 16.5l8.5 4.5 8.5-4.5"/>',
    "shield": '<path d="M12 3l7.5 3v5.5c0 4.8-3.3 8.2-7.5 9.5-4.2-1.3-7.5-4.7-7.5-9.5V6z"/>',
    "shield-check": '<path d="M12 3l7.5 3v5.5c0 4.8-3.3 8.2-7.5 9.5-4.2-1.3-7.5-4.7-7.5-9.5V6z"/><path d="M9 12l2.2 2.2L15.5 10"/>',
    "shield-alert": '<path d="M12 3l7.5 3v5.5c0 4.8-3.3 8.2-7.5 9.5-4.2-1.3-7.5-4.7-7.5-9.5V6z"/><path d="M12 8.5v4M12 16v.2"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "x": '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    "clock": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    "link": '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    "film": '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M8 4.5v15M16 4.5v15M3.5 9.5H8M3.5 14.5H8M16 9.5h4.5M16 14.5h4.5"/>',
    "music": '<path d="M9 18V6l10-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="16.5" cy="16" r="2.5"/>',
    "archive": '<rect x="3.5" y="4" width="17" height="4.5" rx="1.2"/><path d="M5 8.5V19a1.5 1.5 0 0 0 1.5 1.5h11A1.5 1.5 0 0 0 19 19V8.5M10 12.5h4"/>',
    "file-text": '<path d="M14 3.5H7a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-10z"/><path d="M14 3.5v5h5M8.5 13h7M8.5 16.5h5"/>',
    "package": '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5l8 4.5 8-4.5M12 12v9"/>',
    "file": '<path d="M14 3.5H7a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-10z"/><path d="M14 3.5v5h5"/>',
    "grid": '<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/>',
    "activity": '<path d="M3 12h4l3-7.5 4 15 3-7.5h4"/>',
    "check-circle": '<circle cx="12" cy="12" r="8.5"/><path d="M8.5 12.2l2.3 2.3 4.7-4.7"/>',
    "alert": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.8v5M12 16v.2"/>',
    "refresh": '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4h-4"/>',
    "copy": '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M15 9V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h3"/>',
    "external": '<path d="M14 4h6v6M20 4l-9 9M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>',
    "calendar": '<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M9 3v4M15 3v4"/>',
    "zap": '<path d="M13 3L5 13.5h6L10 21l8-10.5h-6z"/>',
    "globe": '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.5 2.5 3.5 5.5 3.5 8.5s-1 6-3.5 8.5c-2.5-2.5-3.5-5.5-3.5-8.5s1-6 3.5-8.5z"/>',
    "gauge": '<path d="M4.5 16a8 8 0 1 1 15 0"/><path d="M12 13l4-4"/><circle cx="12" cy="13.5" r="1.3"/>',
    "arrow-up": '<path d="M12 19V5M6 11l6-6 6 6"/>',
    "arrow-down": '<path d="M12 5v14M6 13l6 6 6-6"/>',
    "inbox": '<path d="M4 13l2.5-7.5h11L20 13v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/><path d="M4 13h4.5l1.5 2.5h4l1.5-2.5H20"/>',
    "lock": '<rect x="5" y="11" width="14" height="9.5" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
    "broom": '<path d="M19 4l-7.5 7.5M8 10.5l5.5 5.5M8 10.5c-2.5 0-4.5 2-4.5 4.5V20h5c2.5 0 4.5-2 4.5-4.5"/>',
}
_cache = {}


def pixmap(name, color=None, size=18, ratio=2.0):
    color = color or C["text"]
    key = (name, color, size, ratio)
    if key not in _cache:
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
               f'<g {_S.format(c=color)}>{ICONS[name]}</g></svg>')
        r = QSvgRenderer(QByteArray(svg.encode()))
        pm = QPixmap(int(size * ratio), int(size * ratio))
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        r.render(p)
        p.end()
        pm.setDevicePixelRatio(ratio)
        _cache[key] = pm
    return _cache[key]


def icon(name, color=None, size=18):
    ic = QIcon()
    ic.addPixmap(pixmap(name, color, size))
    return ic


def accent_gradient(rect, a=None, b=None):
    g = QLinearGradient(rect.topLeft(), rect.topRight())
    g.setColorAt(0, QColor(a or C["accent"]))
    g.setColorAt(1, QColor(b or C["accent2"]))
    return g


def app_icon(size=256):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    r = QRectF(size * 0.06, size * 0.06, size * 0.88, size * 0.88)
    g = QLinearGradient(r.topLeft(), r.bottomRight())
    g.setColorAt(0, QColor(C["accent"]))
    g.setColorAt(1, QColor(C["accent2"]))
    path = QPainterPath()
    path.addRoundedRect(r, size * 0.24, size * 0.24)
    p.fillPath(path, g)
    pen = QPen(QColor("white"), size * 0.085, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    cx = size / 2
    p.drawLine(QPointF(cx, size * 0.25), QPointF(cx, size * 0.6))
    p.drawPolyline([QPointF(cx - size * 0.16, size * 0.45), QPointF(cx, size * 0.61),
                    QPointF(cx + size * 0.16, size * 0.45)])
    p.drawLine(QPointF(size * 0.3, size * 0.74), QPointF(size * 0.7, size * 0.74))
    p.end()
    return QIcon(pm)


QSS = f"""
* {{ outline: 0; }}
QWidget {{ background: {C['bg']}; color: {C['text']}; font-size: 13px; }}
QToolTip {{ background: {C['raised']}; color: {C['text']}; border: 1px solid {C['border2']};
            padding: 6px 8px; border-radius: 6px; }}
QFrame#Sidebar {{ background: {C['panel']}; border-right: 1px solid {C['border']}; }}
QFrame#Sidebar QLabel, QFrame#Details QLabel, QFrame#Panel QLabel, QFrame#Footer QLabel,
QFrame#Toast QLabel, QFrame#SideCard QLabel {{ background: transparent; }}
QWidget#Clear, QFrame#Details QScrollArea, QFrame#Details QStackedWidget {{ background: transparent; }}
QTreeView::item, QListView#Trusted::item {{ padding: 5px 4px; }}
QTreeView::indicator {{ width: 15px; height: 15px; border-radius: 4px;
    border: 1px solid {C['border2']}; background: {C['bg']}; }}
QTreeView::indicator:checked {{ background: {C['accent']}; border-color: {C['accent']}; }}
QTreeView::item:selected {{ background: {C['raised']}; }}
QLabel#Brand {{ font-size: 17px; font-weight: 700; letter-spacing: 0.3px; }}
QLabel#ProChip {{ color: white; font-size: 10px; font-weight: 700; padding: 2px 7px;
                  border-radius: 8px; background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                  stop:0 {C['accent']}, stop:1 {C['accent2']}); }}
QLabel#Section {{ color: {C['faint']}; font-size: 10.5px; font-weight: 700;
                  letter-spacing: 1.2px; padding: 14px 14px 6px 14px; }}
QPushButton#Nav {{ text-align: left; padding: 8px 12px; border-radius: 9px; border: none;
                   color: {C['muted']}; font-weight: 500; background: transparent; }}
QPushButton#Nav:hover {{ background: {C['card']}; color: {C['text']}; }}
QPushButton#Nav:checked {{ background: {C['raised']}; color: {C['text']}; font-weight: 600; }}
QFrame#SideCard {{ background: {C['card']}; border: 1px solid {C['border']}; border-radius: 12px; }}
QLabel#BigNumber {{ font-size: 22px; font-weight: 700; }}
QLabel#Muted {{ color: {C['muted']}; }}
QLabel#Faint {{ color: {C['faint']}; font-size: 12px; }}
QLabel#Title {{ font-size: 22px; font-weight: 700; }}
QLabel#H2 {{ font-size: 15px; font-weight: 650; }}
QLabel#FieldLabel {{ color: {C['muted']}; font-size: 12px; font-weight: 600; }}

QPushButton {{ background: {C['card']}; border: 1px solid {C['border2']}; border-radius: 9px;
               padding: 8px 14px; color: {C['text']}; font-weight: 500; }}
QPushButton:hover {{ background: {C['raised']}; border-color: #3a425a; }}
QPushButton:pressed {{ background: {C['border']}; }}
QPushButton:disabled {{ color: {C['faint']}; border-color: {C['border']}; }}
QPushButton#Primary {{ border: none; color: white; font-weight: 650; padding: 9px 16px;
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {C['accent']}, stop:1 #5b8cff); }}
QPushButton#Primary:hover {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
    stop:0 #8b7dff, stop:1 #6d9aff); }}
QPushButton#Primary:disabled {{ background: {C['border2']}; color: {C['muted']}; }}
QPushButton#Ghost {{ background: transparent; border: 1px solid transparent; color: {C['muted']}; }}
QPushButton#Ghost:hover {{ background: {C['card']}; color: {C['text']}; border-color: {C['border']}; }}
QPushButton#Danger {{ background: rgba(244,63,94,0.12); border: 1px solid rgba(244,63,94,0.35);
                      color: #fda4af; }}
QPushButton#Danger:hover {{ background: rgba(244,63,94,0.22); }}
QPushButton#Icon {{ padding: 7px; border-radius: 8px; background: transparent;
                    border: 1px solid transparent; }}
QPushButton#Icon:hover {{ background: {C['raised']}; border-color: {C['border']}; }}

QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTimeEdit {{
    background: {C['panel']}; border: 1px solid {C['border2']}; border-radius: 9px;
    padding: 7px 10px; selection-background-color: {C['accent']}; color: {C['text']}; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QTimeEdit:focus {{
    border: 1px solid {C['accent']}; }}
QLineEdit#Search {{ padding-left: 34px; border-radius: 10px; background: {C['card']};
                    border-color: {C['border']}; }}
QLineEdit:disabled {{ color: {C['faint']}; }}
QSpinBox::up-button, QSpinBox::down-button, QTimeEdit::up-button, QTimeEdit::down-button {{
    width: 0; border: none; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {C['raised']}; border: 1px solid {C['border2']};
    selection-background-color: {C['accent']}; border-radius: 8px; padding: 4px; }}
QCheckBox {{ spacing: 10px; background: transparent; }}
QCheckBox::indicator {{ width: 34px; height: 19px; border-radius: 10px;
    background: {C['border2']}; image: url(none); }}
QCheckBox::indicator:checked {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
    stop:0 {C['accent']}, stop:1 #5b8cff); }}
QCheckBox::indicator:disabled {{ background: {C['border']}; }}
QRadioButton {{ background: transparent; spacing: 8px; }}

QListView#Downloads {{ background: transparent; border: none; }}
QListView#Downloads::item {{ background: transparent; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
QScrollBar::handle:vertical {{ background: {C['border2']}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #3d4560; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ height: 0; }}

QFrame#Details {{ background: {C['panel']}; border-left: 1px solid {C['border']}; }}
QFrame#Panel {{ background: {C['card']}; border: 1px solid {C['border']}; border-radius: 12px; }}
QFrame#Banner {{ border-radius: 10px; }}
QFrame#Banner QLabel {{ background: transparent; }}
QFrame#Footer {{ background: {C['panel']}; border-top: 1px solid {C['border']}; }}
QFrame#Footer QLabel {{ color: {C['muted']}; font-size: 12px; }}
QFrame#Toast {{ background: {C['raised']}; border: 1px solid {C['border2']}; border-radius: 12px; }}

QMenu {{ background: {C['raised']}; border: 1px solid {C['border2']}; border-radius: 10px;
         padding: 6px; }}
QMenu::item {{ padding: 7px 18px 7px 12px; border-radius: 6px; background: transparent; }}
QMenu::item:selected {{ background: {C['accent']}; color: white; }}
QMenu::item:disabled {{ color: {C['faint']}; }}
QMenu::separator {{ height: 1px; background: {C['border2']}; margin: 5px 6px; }}
QMenu::icon {{ padding-left: 8px; }}

QDialog {{ background: {C['bg']}; }}
QListWidget#SettingsNav {{ background: {C['panel']}; border: none; border-right: 1px solid {C['border']};
                           padding: 10px 8px; }}
QListWidget#SettingsNav::item {{ padding: 9px 12px; border-radius: 8px; color: {C['muted']};
                                 margin-bottom: 2px; }}
QListWidget#SettingsNav::item:hover {{ background: {C['card']}; color: {C['text']}; }}
QListWidget#SettingsNav::item:selected {{ background: {C['raised']}; color: {C['text']}; }}
QListWidget#Trusted {{ background: {C['panel']}; border: 1px solid {C['border2']};
                       border-radius: 9px; padding: 4px; }}
QStackedWidget {{ background: transparent; }}
QTreeView::item, QListView#Trusted::item {{ padding: 5px 4px; }}
QTreeView::indicator {{ width: 15px; height: 15px; border-radius: 4px;
    border: 1px solid {C['border2']}; background: {C['bg']}; }}
QTreeView::indicator:checked {{ background: {C['accent']}; border-color: {C['accent']}; }}
QTreeView::item:selected {{ background: {C['raised']}; }}
"""
