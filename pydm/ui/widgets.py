"""Custom-painted widgets."""
import os
import time

from PySide6.QtCore import (QEasingCurve, QEvent, QObject, QPointF, QPropertyAnimation, QRectF,
                            QSize, Qt, QTimer, Property)
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QCheckBox, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QPushButton, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

from ..core.util import human_size, human_time
from .theme import (C, CAT_COLORS, CAT_ICONS, LEVEL_COLORS, STATUS_STYLE, accent_gradient,
                    pixmap)


def alpha(color, a):
    c = QColor(color)
    c.setAlpha(a)
    return c


def time_ago(ts):
    if not ts:
        return ""
    s = time.time() - ts
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    return time.strftime("%b %d", time.localtime(ts))


def scaled(font, factor, weight=None):
    f = QFont(font)
    if f.pixelSize() > 0:
        f.setPixelSize(max(1, round(f.pixelSize() * factor)))
    else:
        f.setPointSizeF(max(1.0, f.pointSizeF() * factor))
    if weight is not None:
        f.setWeight(weight)
    return f


def status_text(d):
    if d.status == "Completed":
        lvl = (d.security or {}).get("level")
        if lvl == "danger":
            return "Threat", C["danger"]
        if lvl == "caution":
            return "Review", C["warn"]
        return "Done", C["success"]
    if d.status == "Downloading":
        return f"{d.progress * 100:.0f}%", C["accent2"]
    if d.status == "Error" and d.retry_at:
        return "Retrying", C["warn"]
    return d.status, STATUS_STYLE.get(d.status, C["muted"])


def tile_pixmap(category, size=44, ratio=2.0):
    color = CAT_COLORS.get(category, CAT_COLORS["Other"])
    pm = QPixmap(int(size * ratio), int(size * ratio))
    pm.fill(Qt.transparent)
    pm.setDevicePixelRatio(ratio)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), size * 0.26, size * 0.26)
    p.fillPath(path, alpha(color, 40))
    ic = pixmap(CAT_ICONS.get(category, "file"), color, int(size * 0.46))
    s = size * 0.46
    p.drawPixmap(QRectF((size - s) / 2, (size - s) / 2, s, s), ic, QRectF(ic.rect()))
    p.end()
    return pm


# --------------------------------------------------------------------------- toggle
class Toggle(QCheckBox):
    """iOS-style switch."""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self._pos = 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._animate)

    def _get(self):
        return self._pos

    def _set(self, v):
        self._pos = v
        self.update()

    knob = Property(float, _get, _set)

    def setChecked(self, v):
        super().setChecked(v)
        self._pos = 1.0 if v else 0.0

    def _animate(self, on):
        self._anim.stop()
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def sizeHint(self):
        fm = QFontMetrics(self.font())
        w = 38 + (fm.horizontalAdvance(self.text()) + 12 if self.text() else 0)
        return QSize(w, 24)

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        track = QRectF(0, (self.height() - 20) / 2, 36, 20)
        path = QPainterPath()
        path.addRoundedRect(track, 10, 10)
        p.fillPath(path, QColor(C["border2"]))
        if self._pos > 0:
            on = QPainterPath()
            on.addRoundedRect(track, 10, 10)
            p.setOpacity(self._pos if self.isEnabled() else 0.4)
            p.fillPath(on, accent_gradient(track, C["accent"], "#5b8cff"))
            p.setOpacity(1)
        x = track.left() + 2 + self._pos * 16
        p.setBrush(QColor("white") if self.isEnabled() else QColor(C["muted"]))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(x, track.top() + 2, 16, 16))
        if self.text():
            p.setPen(QColor(C["text"] if self.isEnabled() else C["faint"]))
            p.drawText(QRectF(48, 0, self.width() - 48, self.height()),
                       Qt.AlignVCenter | Qt.AlignLeft, self.text())
        p.end()


# --------------------------------------------------------------------------- list cards
class DownloadDelegate(QStyledItemDelegate):
    HEIGHT = 84

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.HEIGHT)

    def paint(self, p, option, index):
        d = index.data(Qt.UserRole)
        if d is None:
            return
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(option.rect).adjusted(2, 4, -8, -4)
        selected = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver)
        path = QPainterPath()
        path.addRoundedRect(r, 12, 12)
        p.fillPath(path, QColor(C["card_hover"] if (hover or selected) else C["card"]))
        p.setPen(QPen(QColor(C["accent"]) if selected else QColor(C["border"]),
                      1.4 if selected else 1))
        p.drawPath(path)

        tile = QRectF(r.left() + 14, r.center().y() - 22, 44, 44)
        p.drawPixmap(tile, tile_pixmap(d.category), QRectF(0, 0, 88, 88))

        chip_text, chip_color = status_text(d)
        base = QFont(option.font)
        chip_font = scaled(base, 0.88, QFont.DemiBold)
        cfm = QFontMetrics(chip_font)
        cw = cfm.horizontalAdvance(chip_text) + 20
        chip = QRectF(r.right() - 16 - cw, r.top() + 14, cw, 22)
        cp = QPainterPath()
        cp.addRoundedRect(chip, 11, 11)
        p.fillPath(cp, alpha(chip_color, 34))
        p.setFont(chip_font)
        p.setPen(QColor(chip_color))
        p.drawText(chip, Qt.AlignCenter, chip_text)

        x = tile.right() + 14
        name_font = scaled(base, 1.06, QFont.DemiBold)
        p.setFont(name_font)
        nfm = QFontMetrics(name_font)
        level = (d.security or {}).get("level")
        badge = level in ("caution", "danger")
        avail = chip.left() - x - 12 - (22 if badge else 0)
        name = nfm.elidedText(d.filename, Qt.ElideMiddle, int(avail))
        p.setPen(QColor(C["text"]))
        name_rect = QRectF(x, r.top() + 13, avail, 22)
        p.drawText(name_rect, Qt.AlignVCenter | Qt.AlignLeft, name)
        if badge:
            bx = x + nfm.horizontalAdvance(name) + 8
            pm = pixmap("shield-alert", LEVEL_COLORS[level], 16)
            p.drawPixmap(QRectF(bx, r.top() + 16, 16, 16), pm, QRectF(pm.rect()))

        meta_font = scaled(base, 0.92)
        p.setFont(meta_font)
        meta, meta_color = self._meta(d)
        mfm = QFontMetrics(meta_font)
        p.setPen(QColor(meta_color))
        p.drawText(QRectF(x, r.top() + 36, r.right() - x - 16, 18), Qt.AlignVCenter | Qt.AlignLeft,
                   mfm.elidedText(meta, Qt.ElideRight, int(r.right() - x - 16)))

        bar = QRectF(x, r.bottom() - 17, r.right() - x - 16, 5)
        bp = QPainterPath()
        bp.addRoundedRect(bar, 2.5, 2.5)
        p.fillPath(bp, QColor(C["border"]))
        frac = d.progress
        if d.kind == "file" and d.total is None and d.status == "Downloading":
            frac = 0.0
        if frac > 0:
            fill = QRectF(bar.left(), bar.top(), max(5, bar.width() * frac), bar.height())
            fp = QPainterPath()
            fp.addRoundedRect(fill, 2.5, 2.5)
            if d.status == "Completed":
                brush = QBrush(QColor(LEVEL_COLORS.get(level, C["success"])
                                      if level in ("caution", "danger") else C["success"]))
            elif d.status == "Error":
                brush = QBrush(QColor(C["danger"]))
            elif d.status in ("Paused", "Queued"):
                brush = QBrush(QColor(C["faint"]))
            else:
                brush = QBrush(accent_gradient(bar))
            p.fillPath(fp, brush)
        p.restore()

    @staticmethod
    def _meta(d):
        done, total = d.downloaded, d.total
        if d.status == "Completed":
            where = os.path.basename(os.path.dirname(d.path or "")) or ""
            return (f"{human_size(total)}   ·   {time_ago(d.finished)}"
                    f"{'   ·   ' + where if where else ''}", C["muted"])
        if d.status == "Error" and d.retry_at:
            secs = max(0, int(d.retry_at - time.time()))
            return f"{d.error or 'Connection problem'}   ·   retrying in {secs}s", C["warn"]
        if d.status == "Error":
            return d.error or "Failed", "#fb7185"
        if d.status == "Extracting":
            return "Extracting archive safely…", C["info"]
        if d.status == "Merging":
            return "Joining video and audio…", C["info"]
        if d.status == "Verifying":
            return "Verifying integrity & scanning for threats…", C["info"]
        if d.status == "Connecting":
            return "Connecting…", C["muted"]
        if d.status == "Downloading":
            exp = d.expected_total
            parts = [f"{human_size(done)} of {'~' if d.kind == 'hls' else ''}{human_size(exp)}"
                     if exp else human_size(done), f"{human_size(d.speed)}/s"]
            if d.kind == "hls":
                parts.insert(1, f"{d.hls_done}/{d.hls_total} segments")
            elif d.kind == "media":
                parts.insert(1, "video + audio" if (d.media or {}).get("op") == "merge" else "audio")
            if d.eta is not None:
                parts.append(f"{human_time(d.eta)} left")
            conns = f"{d.active_connections} connections"
            if d.auto_tune:
                conns += " (auto)"
            parts.append(conns)
            if len(d.sources) > 1:
                parts.append(f"{len(d.sources)} sources")
            return "   ·   ".join(parts), C["muted"]
        if d.status == "Paused":
            exp = d.expected_total
            return (f"Paused at {human_size(done)}" + (f" of {human_size(exp)}" if exp else ""),
                    C["muted"])
        return "Waiting in queue", C["muted"]


# --------------------------------------------------------------------------- details graphics
class SegmentMap(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(18)
        self.d = None

    def set_download(self, d):
        self.d = d
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0, 2, 0, -2)
        clip = QPainterPath()
        clip.addRoundedRect(r, 5, 5)
        p.setClipPath(clip)
        p.fillRect(r, QColor(C["border"]))
        d = self.d
        if d:
            if d.status == "Completed":
                p.fillRect(r, QColor(C["success"]))
            elif d.kind != "file":
                p.fillRect(QRectF(r.left(), r.top(), r.width() * d.progress, r.height()),
                           QBrush(accent_gradient(r)) if d.is_running()
                           else QBrush(QColor(C["faint"])))
            elif d.total:
                with d.lock:
                    segs = [(s.start, s.end, s.done, s.active) for s in d.segments]
                scale = r.width() / d.total
                grad = accent_gradient(r)
                for start, end, done, active in segs:
                    x0 = r.left() + start * scale
                    xd = r.left() + (start + done) * scale
                    x1 = r.left() + ((end if end is not None else start) + 1) * scale
                    if xd > x0:
                        p.fillRect(QRectF(x0, r.top(), xd - x0, r.height()),
                                   QBrush(grad) if d.status == "Downloading"
                                   else QBrush(QColor(C["faint"])))
                    if active:
                        p.fillRect(QRectF(xd - 1.5, r.top(), 3, r.height()), QColor("white"))
                    p.fillRect(QRectF(x1 - 0.5, r.top(), 1, r.height()), QColor(C["panel"]))
        p.end()


class ConnBars(QWidget):
    """One row per live connection: id, speed bar, speed, restarts."""
    ROW = 19
    MAX_ROWS = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows, self.more = [], 0
        self.setFixedHeight(0)

    def set_rows(self, rows):
        rows = sorted(rows, key=lambda r: -r[2])
        self.rows, self.more = rows[:self.MAX_ROWS], max(0, len(rows) - self.MAX_ROWS)
        self.setFixedHeight(len(self.rows) * self.ROW + (self.ROW if self.more else 0))
        self.update()

    def paintEvent(self, _e):
        if not self.rows:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        f = scaled(self.font(), 0.85)
        p.setFont(f)
        top = max(r[2] for r in self.rows) or 1
        w = self.width()
        for i, (wid, host, speed, restarts, primary) in enumerate(self.rows):
            y = i * self.ROW
            p.setPen(QColor(C["faint"]))
            p.drawText(QRectF(0, y, 34, self.ROW), Qt.AlignVCenter, f"#{wid}")
            track = QRectF(36, y + self.ROW / 2 - 3, w - 36 - 96, 6)
            tp = QPainterPath()
            tp.addRoundedRect(track, 3, 3)
            p.fillPath(tp, QColor(C["border"]))
            fill = QRectF(track.left(), track.top(), max(3, track.width() * speed / top), 6)
            fp = QPainterPath()
            fp.addRoundedRect(fill, 3, 3)
            p.fillPath(fp, QBrush(accent_gradient(track)) if primary
                       else QBrush(QColor("#f59e0b")))
            p.setPen(QColor(C["muted"]))
            label = f"{human_size(speed)}/s" + (f"  ↻{restarts}" if restarts else "")
            p.drawText(QRectF(w - 92, y, 92, self.ROW), Qt.AlignVCenter | Qt.AlignRight, label)
        if self.more:
            p.setPen(QColor(C["faint"]))
            p.drawText(QRectF(0, len(self.rows) * self.ROW, w, self.ROW), Qt.AlignVCenter,
                       f"+ {self.more} more connections")
        p.end()


class Sparkline(QWidget):
    def __init__(self, parent=None, height=64):
        super().__init__(parent)
        self.setFixedHeight(height)
        self.values = []

    def set_values(self, values):
        self.values = list(values)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 4, -1, -2)
        p.setPen(QPen(QColor(C["border"]), 1, Qt.DashLine))
        for i in range(1, 3):
            y = r.top() + r.height() * i / 3
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        vals = self.values
        peak = max(vals) if vals else 0
        if len(vals) >= 2 and peak > 0:
            step = r.width() / (len(vals) - 1)
            pts = [QPointF(r.left() + i * step, r.bottom() - (v / peak) * r.height() * 0.92)
                   for i, v in enumerate(vals)]
            line = QPainterPath(pts[0])
            for a, b in zip(pts, pts[1:]):
                mid = (a.x() + b.x()) / 2
                line.cubicTo(QPointF(mid, a.y()), QPointF(mid, b.y()), b)
            area = QPainterPath(line)
            area.lineTo(r.right(), r.bottom())
            area.lineTo(r.left(), r.bottom())
            area.closeSubpath()
            g = QLinearGradient(r.topLeft(), r.bottomLeft())
            g.setColorAt(0, alpha(C["accent"], 110))
            g.setColorAt(1, alpha(C["accent"], 0))
            p.fillPath(area, g)
            p.setPen(QPen(QBrush(accent_gradient(r)), 2))
            p.drawPath(line)
        p.end()


# --------------------------------------------------------------------------- banner & chip
class Banner(QFrame):
    ICON = {"info": "check-circle", "safe": "shield-check", "caution": "alert",
            "danger": "shield-alert"}

    def __init__(self, level, text, parent=None):
        super().__init__(parent)
        self.setObjectName("Banner")
        color = LEVEL_COLORS.get(level, C["info"])
        c = QColor(color)
        self.setStyleSheet(f"QFrame#Banner {{ background: rgba({c.red()},{c.green()},{c.blue()},0.10);"
                           f" border: 1px solid rgba({c.red()},{c.green()},{c.blue()},0.35); }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)
        ic = QLabel()
        ic.setPixmap(pixmap(self.ICON.get(level, "alert"), color, 18))
        ic.setAlignment(Qt.AlignTop)
        lay.addWidget(ic)
        lab = QLabel(text)
        lab.setWordWrap(True)
        lab.setStyleSheet("color: #dfe3ec;")
        lay.addWidget(lab, 1)


def chip_style(color):
    c = QColor(color)
    return (f"color: {color}; background: rgba({c.red()},{c.green()},{c.blue()},0.14);"
            f" border-radius: 10px; padding: 3px 10px; font-weight: 600; font-size: 11.5px;")


# --------------------------------------------------------------------------- toasts
class Toast(QFrame):
    def __init__(self, host, title, text, level="info", actions=None, timeout=6000):
        super().__init__(host.parent_widget)
        self.setObjectName("Toast")
        self.host = host
        self.setFixedWidth(360)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 12, 10, 12)
        lay.setSpacing(12)
        ic = QLabel()
        ic.setPixmap(pixmap(Banner.ICON.get(level, "alert"), LEVEL_COLORS.get(level, C["info"]), 20))
        ic.setAlignment(Qt.AlignTop)
        lay.addWidget(ic)
        col = QVBoxLayout()
        col.setSpacing(3)
        t = QLabel(title)
        t.setStyleSheet("font-weight: 650;")
        col.addWidget(t)
        if text:
            b = QLabel(text)
            b.setWordWrap(True)
            b.setStyleSheet(f"color: {C['muted']}; font-size: 12px;")
            col.addWidget(b)
        if actions:
            row = QHBoxLayout()
            row.setSpacing(6)
            row.setContentsMargins(0, 6, 0, 0)
            for i, (label, cb) in enumerate(actions):
                btn = QPushButton(label)
                btn.setObjectName("Primary" if i == 0 else "Ghost")
                btn.setCursor(Qt.PointingHandCursor)
                btn.clicked.connect(lambda _=False, cb=cb: (cb(), self.dismiss()))
                row.addWidget(btn)
            row.addStretch()
            col.addLayout(row)
        lay.addLayout(col, 1)
        close = QPushButton()
        close.setObjectName("Icon")
        close.setIcon(pixmap("x", C["muted"], 14))
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self.dismiss)
        lay.addWidget(close, 0, Qt.AlignTop)
        self.effect = QGraphicsOpacityEffect(self)
        self.effect.setOpacity(0)
        self.setGraphicsEffect(self.effect)
        self.anim = QPropertyAnimation(self.effect, b"opacity", self)
        self.anim.setDuration(180)
        self.anim.setEndValue(1)
        self.anim.start()
        QTimer.singleShot(timeout, self.dismiss)
        self.adjustSize()
        self.show()
        self.raise_()

    def dismiss(self):
        if getattr(self, "_closing", False):
            return
        self._closing = True
        self.anim.stop()
        self.anim.setEndValue(0)
        self.anim.finished.connect(lambda: self.host.remove(self))
        self.anim.start()


class ToastHost(QObject):
    def __init__(self, parent_widget):
        super().__init__(parent_widget)
        self.parent_widget = parent_widget
        self.toasts = []
        parent_widget.installEventFilter(self)

    def show(self, title, text="", level="info", actions=None, timeout=None):
        t = Toast(self, title, text, level, actions, timeout or (12000 if actions else 6000))
        self.toasts.append(t)
        while len([x for x in self.toasts if not getattr(x, "_closing", False)]) > 3:
            next(x for x in self.toasts if not getattr(x, "_closing", False)).dismiss()
        self.layout()
        return t

    def remove(self, t):
        if t in self.toasts:
            self.toasts.remove(t)
        t.deleteLater()
        self.layout()

    def layout(self):
        pw = self.parent_widget
        y = pw.height() - 20
        for t in reversed(self.toasts):
            t.adjustSize()
            y -= t.height()
            t.move(pw.width() - t.width() - 20, y)
            t.raise_()
            y -= 10

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Resize:
            self.layout()
        return False
