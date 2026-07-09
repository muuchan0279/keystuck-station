#!/usr/bin/env python3
"""KEYSTUCK STATION — Proton/Wine prefix の DisableHidraw 適用状況を一覧し、行ごとに適用/戻す。

背景: winebus の hidraw プローブが ASUS 内蔵KB(0b05:19b6)を刺して USB リセットを連発し、
全アプリでキー詰まりが起きる。prefix の system.reg に DisableHidraw=1 を入れると止まる。
prefix は新規ゲームごとに作られるので「どこに穴が空いているか」を台帳で見る。
"""

import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

APP_ID = "keystuck-station"
STATE_DIR = os.path.expanduser(f"~/.config/{APP_ID}")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

# Steam はネイティブ / Flatpak / 旧 ~/.steam で場所が違う。存在するものを全部見る
STEAM_ROOTS = [
    "~/.local/share/Steam",
    "~/.steam/steam",
    "~/.var/app/com.valvesoftware.Steam/data/Steam",   # Flatpak
]
# Steam 以外のランチャーが作る prefix。glob 可
EXTRA_PREFIX_GLOBS = [
    "~/.xlcore/wineprefix",                            # XIVLauncher
    "~/Games/*/prefix",                                # Lutris 既定
    "~/.var/app/com.usebottles.bottles/data/bottles/bottles/*",
]

BAK_SUFFIX = ".bak-keystuck"

# ---------------------------------------------------------------- palette
BG        = "#14161b"
PANEL     = "#1c2029"
PANEL_HI  = "#242a35"
TEXT      = "#d7dbe0"
DIM       = "#6b7280"
NEON_GRN  = "#39ff14"
NEON_AMB  = "#ffb300"
NEON_RED  = "#ff2d55"
NEON_CYAN = "#00e5ff"
NEON_MAG  = "#ff4dd8"

FRAME_THEME = os.path.expanduser("~/.config/muu-widgets/frame-theme.json")


def read_frame_theme():
    default = {"mode": "neon", "neon": {"color": "#00e5ff", "width": 2},
               "win9x": {"face": "#c0c0c0", "highlight": "#ffffff", "shadow": "#404040", "width": 3}}
    try:
        t = json.load(open(FRAME_THEME, encoding="utf-8"))
        for k, v in default.items():
            t.setdefault(k, v)
        return t
    except Exception:
        return default


def frame_rule(bg):
    t = read_frame_theme()
    if t.get("mode") == "win9x":
        w = t["win9x"]; n = int(w.get("width", 3))
        return ("#frame {{ background:{bg}; "
                "border-top:{n}px solid {hi}; border-left:{n}px solid {hi}; "
                "border-right:{n}px solid {sh}; border-bottom:{n}px solid {sh}; }}").format(
                    bg=bg, n=n, hi=w["highlight"], sh=w["shadow"])
    n = t["neon"]
    return "#frame {{ background:{bg}; border:{w}px solid {c}; }}".format(
        bg=bg, w=int(n.get("width", 2)), c=n.get("color", "#00e5ff"))


def _theme_mtime():
    try:
        return os.path.getmtime(FRAME_THEME)
    except OSError:
        return 0


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


# ---------------------------------------------------------------- prefix model
WINEBUS_RE = re.compile(
    r'(\[System\\\\ControlSet001\\\\Services\\\\winebus\][^\n]*\n(?:#time=[^\n]*\n)?)')


def steam_libraries():
    """全 Steam ルートの libraryfolders.vdf を読み、別ドライブのライブラリも拾う。"""
    libs = []
    for root in STEAM_ROOTS:
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            continue
        sa_root = os.path.join(root, "steamapps")
        vdf = os.path.join(sa_root, "libraryfolders.vdf")
        found = False
        try:
            text = open(vdf, encoding="utf-8", errors="replace").read()
            for path in re.findall(r'"path"\s+"([^"]+)"', text):
                sa = os.path.join(path, "steamapps")
                if os.path.isdir(sa):
                    libs.append(sa)
                    found = True
        except OSError:
            pass
        if not found and os.path.isdir(sa_root):
            libs.append(sa_root)
    # ~/.steam/steam は ~/.local/share/Steam へのシンボリックリンクのことが多い
    seen, uniq = set(), []
    for sa in libs:
        real = os.path.realpath(sa)
        if real not in seen:
            seen.add(real)
            uniq.append(sa)
    return uniq


def app_name(steamapps, appid):
    acf = os.path.join(steamapps, f"appmanifest_{appid}.acf")
    try:
        text = open(acf, encoding="utf-8", errors="replace").read()
        m = re.search(r'"name"\s+"([^"]+)"', text)
        if m:
            return m.group(1)
    except OSError:
        pass
    return f"appid {appid}"


def running_prefixes():
    """稼働中 wineserver の WINEPREFIX 集合。ここを編集すると終了時に巻き戻される。"""
    live = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                if f.read().strip() != "wineserver":
                    continue
            with open(f"/proc/{pid}/environ", "rb") as f:
                env = f.read().split(b"\0")
        except OSError:
            continue
        for entry in env:
            if entry.startswith(b"WINEPREFIX="):
                p = entry[len(b"WINEPREFIX="):].decode("utf-8", "replace")
                live.add(os.path.realpath(p))
    return live


def prefix_status(reg_path):
    """APPLIED / MISSING / NO_SECTION を返す。"""
    try:
        text = open(reg_path, encoding="utf-8", errors="replace").read()
    except OSError:
        return "MISSING"
    m = WINEBUS_RE.search(text)
    if not m:
        return "NO_SECTION"
    sec_end = text.find("\n[", m.end())
    if sec_end == -1:
        sec_end = len(text)
    return "APPLIED" if '"DisableHidraw"' in text[m.start():sec_end] else "MISSING"


def scan_prefixes():
    rows = []
    for sa in steam_libraries():
        compat = os.path.join(sa, "compatdata")
        if not os.path.isdir(compat):
            continue
        for appid in sorted(os.listdir(compat)):
            if appid == "0":      # Steam 自身の入れ物。ゲームの prefix ではない
                continue
            reg = os.path.join(compat, appid, "pfx", "system.reg")
            if not os.path.isfile(reg):
                continue
            rows.append({"name": app_name(sa, appid), "tag": appid,
                         "prefix": os.path.join(compat, appid, "pfx"), "reg": reg})
    for pattern in EXTRA_PREFIX_GLOBS:
        for pfx in sorted(glob.glob(os.path.expanduser(pattern))):
            reg = os.path.join(pfx, "system.reg")
            if not os.path.isfile(reg):
                continue
            if "xlcore" in pfx:
                name, tag = "FFXIV (XIVLauncher)", "xlcore"
            elif "bottles" in pfx:
                name, tag = os.path.basename(pfx), "Bottles"
            else:
                name, tag = os.path.basename(os.path.dirname(pfx)), "Lutris"
            rows.append({"name": name, "tag": tag, "prefix": pfx, "reg": reg})

    live = running_prefixes()
    for r in rows:
        r["status"] = prefix_status(r["reg"])
        r["live"] = os.path.realpath(r["prefix"]) in live
        r["has_bak"] = os.path.isfile(r["reg"] + BAK_SUFFIX)
    rows.sort(key=lambda r: (r["status"] == "APPLIED", r["name"].lower()))
    return rows


def apply_patch(reg_path):
    """DisableHidraw=1 を winebus セクションに挿入。バックアップを残す。"""
    text = open(reg_path, encoding="utf-8", errors="replace").read()
    m = WINEBUS_RE.search(text)
    if not m:
        raise RuntimeError("winebus セクションが無い(prefix が未初期化かも)")
    sec_end = text.find("\n[", m.end())
    if sec_end == -1:
        sec_end = len(text)
    if '"DisableHidraw"' in text[m.start():sec_end]:
        return
    with open(reg_path + BAK_SUFFIX, "w", encoding="utf-8") as f:
        f.write(text)
    patched = text[:m.end()] + '"DisableHidraw"=dword:00000001\n' + text[m.end():]
    with open(reg_path, "w", encoding="utf-8") as f:
        f.write(patched)


def revert_patch(reg_path):
    """バックアップがあれば戻し、無ければ該当行だけ削る。"""
    bak = reg_path + BAK_SUFFIX
    if os.path.isfile(bak):
        with open(bak, encoding="utf-8", errors="replace") as f:
            text = f.read()
        with open(reg_path, "w", encoding="utf-8") as f:
            f.write(text)
        os.remove(bak)
        return
    text = open(reg_path, encoding="utf-8", errors="replace").read()
    text = re.sub(r'^"DisableHidraw"=dword:00000001\n', "", text, flags=re.M)
    with open(reg_path, "w", encoding="utf-8") as f:
        f.write(text)


RESET_RE = re.compile(r"reset .*speed USB device")

# 嵐は 1〜2 秒間隔で来る。60秒窓に5回入っていれば「今まさに」と言い切れる。
# 抜き差し1回で2〜3件は普通に出るので、そこは嵐と呼ばない。
LIVE_WINDOW_SEC = 60
LIVE_THRESHOLD = 5
RECENT_THRESHOLD = 10


def reset_probe(minutes=5):
    """直近のUSBリセットを時刻付きで拾う。

    「5分間に何回」は嵐が終わった後も5分間そう言い続ける。今起きているかは
    直近60秒だけを見ないと分からない。両者は別の数字なので別々に返す。
    """
    try:
        out = subprocess.run(
            ["journalctl", "-k", "--since", f"{minutes} minutes ago",
             "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return None

    stamps = []
    for line in out.splitlines():
        if not RESET_RE.search(line):
            continue
        try:
            stamps.append(datetime.fromisoformat(line.split(None, 1)[0]))
        except (ValueError, IndexError):
            pass

    now = datetime.now().astimezone()
    live = [t for t in stamps
            if (now - t).total_seconds() <= LIVE_WINDOW_SEC]
    return {
        "total": len(stamps),
        "live": len(live),
        "last": stamps[-1] if stamps else None,
        "minutes": minutes,
        "at": now,
    }


def storm_banner(p):
    """probe結果 -> (バナー文, 色)。GUI から切り離して単体で試せるようにしてある。"""
    if p is None:
        return "USBリセット: 読み取り不可(journal権限)", DIM

    stamp = p["at"].strftime("%H:%M:%S")
    mins = p["minutes"]

    if p["live"] >= LIVE_THRESHOLD:
        text = (f"⚠ RESET STORM {p['live']}回/{LIVE_WINDOW_SEC}s"
                f" — 今キー詰まりが起きている")
        color = NEON_RED
    elif p["total"] >= RECENT_THRESHOLD:
        last = p["last"].strftime("%H:%M:%S")
        text = (f"◍ 直近{mins}分に {p['total']}回 — 今は収まっている"
                f"(最後 {last})")
        color = NEON_AMB
    elif p["total"] > 0:
        text = (f"USBリセット {p['total']}回/{mins}min"
                f" — 散発(通常の抜き差しかも)")
        color = NEON_AMB
    else:
        text = f"USBリセット 0回/{mins}min — 静穏"
        color = NEON_GRN

    return f"{text}  [{stamp} 時点]", color


HELP_TEXT = """<b>このアプリが書き換えるもの</b><br>
各 prefix の <code>system.reg</code> にある winebus セクションへ
<code>"DisableHidraw"=dword:00000001</code> を1行入れるだけ。<br><br>

<b>なぜ効くのか</b><br>
Wine/Proton の <code>winebus</code> はゲームパッド探しで全 hidraw デバイスを舐める。
これに応答できないUSBデバイス(ASUS N-KEY内蔵キーボード等)があると、カーネルが
1〜2秒ごとに USB リセットを連発し、その窓で KeyRelease が消える。結果、
カーネルより下の層で「押しっぱなし」になり、<b>ゲームだけでなく全アプリ</b>で
キーが詰まる。hidraw プローブを止めればリセットも止まる。<br><br>

<b>⚠ ゲーム起動中は編集できない(ボタンを無効化している)</b><br>
<code>wineserver</code> が生きている間に <code>system.reg</code> を書き換えても、
<b>プロセス終了時にメモリ上のレジストリで丸ごと上書きされ、修正が消える</b>。
だから該当行は <span style="color:#00e5ff">▶ RUNNING</span> と出して押せなくしてある。
ゲームを完全に終了してから <code>↻</code> で再スキャンすること。<br><br>

<b>⚠ 副作用</b><br>
その prefix では hidraw を直接叩くパッド機能(DualSense のアダプティブトリガーや
ハプティクス等)が無効になる。通常のキーボード・通常のゲームパッド入力は無傷。<br><br>

<b>⚠ prefix 再生成で消える</b><br>
ゲームの再インストールや compatdata 削除で修正も消える。新しいゲームを入れるたびに
MISSING が増えるので、時々この台帳を見ること。<br><br>

<b>元に戻すには</b><br>
適用時に <code>system.reg.bak-keystuck</code> を必ず残す。[戻す] でそこから復元する
(バックアップが無い場合は該当行だけ削除)。<br><br>

<b>△ NO BUS</b> は winebus セクションがまだ無い prefix。一度ゲームを起動すれば作られる。
"""


# ---------------------------------------------------------------- resize grip
class ResizeGrip(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(16, 16)
        self.setCursor(Qt.SizeFDiagCursor)
        self.setToolTip("ドラッグでリサイズ")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            win = self.window()
            win._user_sized = True
            wh = win.windowHandle()
            if wh is not None:
                wh.startSystemResize(Qt.BottomEdge | Qt.RightEdge)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setPen(QPen(QColor(DIM), 1))
        w, h = self.width(), self.height()
        for off in (3, 7, 11):
            p.drawLine(w - off, h - 2, w - 2, h - off)
        p.end()


# ---------------------------------------------------------------- row
class ElidingLabel(QLabel):
    """長いゲーム名で行が横に伸びるとボタンが画面外に出るので、末尾を … で詰める。"""

    def __init__(self, text, color):
        super().__init__(text)
        self._full = text
        self._color = color
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(48)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setPen(QColor(self._color))
        fm = self.fontMetrics()
        p.drawText(self.rect(), Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(self._full, Qt.ElideRight, self.width()))
        p.end()


class PrefixRow(QFrame):
    def __init__(self, row, on_action):
        super().__init__()
        self.row = row
        self.setObjectName("card")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 5, 8, 5)
        lay.setSpacing(8)

        name = ElidingLabel(row["name"], NEON_CYAN)
        name.setToolTip(row["prefix"])
        lay.addWidget(name, 1)

        tag = QLabel(row["tag"])
        tag.setObjectName("tag")
        lay.addWidget(tag)

        status = QLabel()
        status.setObjectName("status")
        status.setFixedWidth(96)
        if row["status"] == "APPLIED":
            status.setText("● APPLIED")
            status.setStyleSheet(f"color:{NEON_GRN};")
        elif row["status"] == "NO_SECTION":
            status.setText("△ NO BUS")
            status.setStyleSheet(f"color:{DIM};")
            status.setToolTip("winebus セクションが無い。一度ゲームを起動すると作られる")
        else:
            status.setText("○ MISSING")
            status.setStyleSheet(f"color:{NEON_AMB};")
        lay.addWidget(status)

        btn = QPushButton()
        btn.setFixedWidth(58)
        if row["status"] == "APPLIED":
            btn.setText("戻す")
            btn.setObjectName("revert")
        elif row["status"] == "NO_SECTION":
            btn.setText("—")
            btn.setEnabled(False)
        else:
            btn.setText("適用")
            btn.setObjectName("apply")

        if row["live"]:
            # wineserver 生存中に system.reg を書くと、終了時に丸ごと巻き戻される
            btn.setEnabled(False)
            btn.setToolTip("ゲーム起動中 — 終了してから")
            status.setText("▶ RUNNING")
            status.setStyleSheet(f"color:{NEON_CYAN};")
        btn.clicked.connect(lambda: on_action(row))
        lay.addWidget(btn)


# ---------------------------------------------------------------- main window
class KeystuckStation(QFrame):
    def __init__(self):
        super().__init__()
        self.state = load_state()
        self._rows = []
        self._on_top = bool(self.state.get("on_top", False))
        self._user_sized = False
        self._theme_mtime = _theme_mtime()
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._persist)

        self.setWindowTitle("KEYSTUCK STATION")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnBottomHint)

        self.frame = self
        self.frame.setObjectName("frame")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_titlebar())
        root.addWidget(self._build_banner())
        root.addWidget(self._build_search())
        root.addWidget(self._build_caveat())

        self.list_box = QWidget()
        self.list_lay = QVBoxLayout(self.list_box)
        self.list_lay.setContentsMargins(8, 6, 8, 6)
        self.list_lay.setSpacing(4)
        self.list_lay.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(self.list_box)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        root.addWidget(scroll, 1)

        root.addWidget(self._build_footer())

        self._base_css = self._css()
        self._apply_frame()

        size = self.state.get("size")
        if size:
            self.resize(*size)
            self._user_sized = True
        else:
            self.resize(600, 520)
        pos = self.state.get("pos")
        if pos:
            self.move(*pos)

        self.refresh()

        self.tick = QTimer(self)
        self.tick.timeout.connect(self._tick)
        self.tick.start(1000)

        # 開きっぱなしのバナーが古い嵐を指し続けないよう、journal だけ定期に見直す。
        # 台帳のディスク走査は重いのでここには載せない(↻ 専用)。
        self.storm_tick = QTimer(self)
        self.storm_tick.timeout.connect(self._refresh_banner)
        self.storm_tick.start(20_000)

    # -- chrome ---------------------------------------------------
    def _build_titlebar(self):
        bar = QWidget()
        bar.setFixedHeight(27)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 0, 8, 0)
        lay.setSpacing(8)

        title = QLabel("▣ KEYSTUCK STATION")
        title.setObjectName("title")
        lay.addWidget(title)
        lay.addStretch()

        self.help_btn = QPushButton("?")
        self.help_btn.setObjectName("help")
        self.help_btn.setToolTip("何をするアプリか・注意点")
        self.pin_btn = QPushButton("▼")
        self.pin_btn.setObjectName("pin")
        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setObjectName("refresh")
        self.refresh_btn.setToolTip("台帳を再スキャン + USBリセットを再チェック")
        self.min_btn = QPushButton("─")
        self.min_btn.setObjectName("min")
        close = QPushButton("✕")
        close.setObjectName("close")
        for b in (self.help_btn, self.pin_btn, self.refresh_btn, self.min_btn, close):
            b.setFixedSize(18, 18)
            lay.addWidget(b)

        self.help_btn.clicked.connect(self._show_help)
        self.pin_btn.clicked.connect(self._toggle_depth)
        self.refresh_btn.clicked.connect(self.refresh)
        self.min_btn.clicked.connect(self.showMinimized)
        close.clicked.connect(self.close)

        bar.mousePressEvent = self._start_move
        return bar

    def _build_banner(self):
        self.banner = QLabel()
        self.banner.setObjectName("banner")
        self.banner.setContentsMargins(12, 4, 12, 4)
        return self.banner

    def _build_search(self):
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(12, 2, 12, 4)
        lay.setSpacing(6)

        icon = QLabel("⌕")
        icon.setObjectName("searchicon")
        lay.addWidget(icon)

        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("絞り込み — ゲーム名 / appid")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._rebuild)   # 再スキャンはしない(journalが重い)
        lay.addWidget(self.search, 1)
        return box

    def _build_caveat(self):
        cav = QLabel("⚠ ゲーム起動中の prefix は編集不可 — wineserver 終了時に "
                     "レジストリが巻き戻り修正が消えるため。[?] に詳細と副作用")
        cav.setObjectName("caveat")
        cav.setWordWrap(True)
        cav.setContentsMargins(12, 0, 12, 4)
        return cav

    def _show_help(self):
        box = QMessageBox(self)
        box.setWindowTitle("KEYSTUCK STATION — 読んでから使う")
        box.setTextFormat(Qt.RichText)
        box.setText(HELP_TEXT)
        # QMessageBox は幅をテキストから決めるので、min-width で読める幅に広げる
        box.setStyleSheet(f"QLabel{{ color:{TEXT}; background:{BG}; min-width:560px; }}"
                          f"QMessageBox{{ background:{BG}; }}"
                          f"QPushButton{{ background:{PANEL_HI}; color:{TEXT};"
                          f" border:0; border-radius:0; padding:4px 18px; }}")
        box.exec()

    def _build_footer(self):
        foot = QWidget()
        lay = QHBoxLayout(foot)
        lay.setContentsMargins(12, 0, 3, 2)
        lay.setSpacing(8)

        self.summary = QLabel()
        self.summary.setObjectName("summary")
        lay.addWidget(self.summary)
        lay.addStretch()

        self.all_btn = QPushButton("一括適用")
        self.all_btn.setObjectName("apply")
        self.all_btn.setFixedWidth(84)
        self.all_btn.clicked.connect(self._apply_all)
        lay.addWidget(self.all_btn)
        lay.addWidget(ResizeGrip(), 0, Qt.AlignBottom)
        return foot

    def _css(self):
        return f"""
        QWidget {{ background:{BG}; color:{TEXT};
                   font-family:'JetBrainsMono Nerd Font'; font-size:11px; }}
        QLabel#title {{ color:{NEON_MAG}; font-family:'Terminess Nerd Font';
                        font-size:12px; letter-spacing:1px; }}
        QLabel#banner {{ color:{DIM}; }}
        QLabel#summary {{ color:{DIM}; }}
        QLabel#caveat {{ color:{NEON_AMB}; font-size:10px; }}
        QLabel#searchicon {{ color:{NEON_CYAN}; }}
        QLineEdit#search {{ background:{PANEL}; color:{TEXT}; border:1px solid {DIM};
                            border-radius:0; padding:2px 4px;
                            selection-background-color:{NEON_CYAN};
                            selection-color:{BG}; }}
        QLineEdit#search:focus {{ border:1px solid {NEON_CYAN}; }}
        QLabel#name {{ color:{NEON_CYAN}; }}
        QLabel#tag {{ color:{DIM}; }}
        QFrame#card {{ background:{PANEL}; border:0; }}
        QScrollArea {{ background:{BG}; border:0; }}
        QPushButton {{ background:{PANEL_HI}; color:{TEXT}; border:0;
                       border-radius:0; padding:3px; }}
        QPushButton:hover {{ background:{PANEL}; }}
        QPushButton:disabled {{ color:{DIM}; background:{PANEL}; }}
        QPushButton#apply {{ color:{NEON_GRN}; }}
        QPushButton#revert {{ color:{NEON_AMB}; }}
        QPushButton#help {{ color:{NEON_AMB}; }}
        QPushButton#pin {{ color:{TEXT}; }}
        QPushButton#pin[on="true"] {{ color:{NEON_CYAN}; }}
        QPushButton#refresh {{ color:{NEON_CYAN}; }}
        QPushButton#close {{ color:{NEON_RED}; }}
        """

    def _apply_frame(self):
        self.setStyleSheet(self._base_css + "\n" + frame_rule(BG))
        self.frame.style().unpolish(self.frame)
        self.frame.style().polish(self.frame)
        self.frame.update()

    def _tick(self):
        m = _theme_mtime()
        if m != self._theme_mtime:
            self._theme_mtime = m
            self._apply_frame()

    # -- depth / move / persist -----------------------------------
    def _apply_depth(self):
        geo = self.geometry()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, self._on_top)
        self.setWindowFlag(Qt.WindowStaysOnBottomHint, not self._on_top)
        self.setGeometry(geo)
        self.show()
        if self._on_top:
            self.pin_btn.setText("▲"); self.pin_btn.setToolTip("最背面に戻す")
        else:
            self.pin_btn.setText("▼"); self.pin_btn.setToolTip("最前面に固定")
        self.pin_btn.setProperty("on", self._on_top)
        self.pin_btn.style().unpolish(self.pin_btn)
        self.pin_btn.style().polish(self.pin_btn)

    def _toggle_depth(self):
        self._on_top = not self._on_top
        self._apply_depth()
        self.state["on_top"] = self._on_top
        save_state(self.state)

    def _start_move(self, e):
        if e.button() != Qt.LeftButton:
            return
        wh = self.windowHandle()
        if wh is not None and wh.startSystemMove():
            return

    def _persist(self):
        self.state["pos"] = [self.x(), self.y()]
        if self._user_sized:
            self.state["size"] = [self.width(), self.height()]
        self.state["on_top"] = self._on_top
        save_state(self.state)

    def moveEvent(self, e):
        super().moveEvent(e)
        self._save_timer.start(500)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._user_sized:
            self._save_timer.start(500)

    def closeEvent(self, e):
        self._persist()
        super().closeEvent(e)

    # -- data -----------------------------------------------------
    def _visible_rows(self):
        q = self.search.text().strip().lower()
        if not q:
            return self._rows
        return [r for r in self._rows
                if q in r["name"].lower() or q in r["tag"].lower()]

    def _rebuild(self):
        """検索フィルタを反映して行を描き直す。ディスクもjournalも触らない。"""
        while self.list_lay.count() > 1:
            item = self.list_lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        rows = self._visible_rows()
        for r in rows:
            self.list_lay.insertWidget(self.list_lay.count() - 1, PrefixRow(r, self._on_action))

        missing = [r for r in rows if r["status"] == "MISSING" and not r["live"]]
        applied = sum(1 for r in rows if r["status"] == "APPLIED")
        live = sum(1 for r in rows if r["live"])
        shown = "" if len(rows) == len(self._rows) else f"{len(rows)}/"
        self.summary.setText(
            f"{shown}{len(self._rows)} prefix / APPLIED {applied} / MISSING {len(missing)}"
            + (f" / 起動中 {live}" if live else ""))
        self.all_btn.setText(f"一括適用 {len(missing)}" if missing else "一括適用")
        self.all_btn.setEnabled(bool(missing))

    def refresh(self):
        """↻ の中身。台帳(ディスク)と嵐(journal)の両方を取り直す。"""
        self._rows = scan_prefixes()
        self._rebuild()
        self._refresh_banner()

    def _refresh_banner(self):
        """journal だけ見る軽い方。自動ポーリングもここを叩く。"""
        text, color = storm_banner(reset_probe())
        self.banner.setText(text)
        self.banner.setStyleSheet(f"color:{color};")

    def _on_action(self, row):
        try:
            if row["status"] == "APPLIED":
                revert_patch(row["reg"])
            else:
                apply_patch(row["reg"])
        except Exception as exc:
            QMessageBox.warning(self, "KEYSTUCK STATION", f"{row['name']}:\n{exc}")
        self.refresh()

    def _apply_all(self):
        # 絞り込み中は「見えている行」だけが対象。隠れた prefix を勝手に触らない
        rows = [r for r in self._visible_rows()
                if r["status"] == "MISSING" and not r["live"]]
        if not rows:
            return
        names = "\n".join(f"  • {r['name']}" for r in rows[:12])
        more = f"\n  … 他 {len(rows) - 12} 本" if len(rows) > 12 else ""
        ok = QMessageBox.question(
            self, "一括適用",
            f"{len(rows)} 本の prefix に DisableHidraw を入れる:\n{names}{more}\n\n"
            "各 system.reg のバックアップを残す。",
            QMessageBox.Yes | QMessageBox.No)
        if ok != QMessageBox.Yes:
            return
        failed = []
        for r in rows:
            try:
                apply_patch(r["reg"])
            except Exception as exc:
                failed.append(f"{r['name']}: {exc}")
        if failed:
            QMessageBox.warning(self, "一部失敗", "\n".join(failed))
        self.refresh()


def main():
    QApplication.setDesktopFileName(APP_ID)   # app_idを.desktopに紐付け(アイコン化け対策)
    app = QApplication(sys.argv)
    w = KeystuckStation()
    w.show()
    w._apply_depth()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
