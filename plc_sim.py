#!/usr/bin/env python3
"""
PLC·SIM — SAP EWM MFS Device Simulator
Simulates a PLC/conveyor device communicating with SAP EWM via TCP.

Telegram format (fixed 128-byte ASCII, space-padded):
  [0:2]   Type     : LI | MO | CF | ER
  [2:4]   SubType  : 00–99
  [4:12]  Source   : device/system name (8 chars)
  [12:20] Dest     : target name (8 chars)
  [20:26] Sequence : zero-padded integer (6 chars)
  [26:128] Data    : type-specific fields (102 chars, space-padded)

LIFE  data: empty or "PING"/"PONG"
MOVE  data: [TU:20][SRC_BIN:20][DST_BIN:20][PRIORITY:2][EXTRA:40]
CNFM  data: [TU:20][BIN:20][STATUS:4][TIMESTAMP:14][EXTRA:44]
ERR   data: [ERRCODE:4][ERRMSG:98]
"""

import socket
import threading
import queue
import time
import datetime
import json
import os
import sys
import re

try:
    import customtkinter as ctk
    from customtkinter import CTkFont
    HAS_CTK = True
except ImportError:
    HAS_CTK = False
    print("ERROR: customtkinter not installed.")
    print("Run:  pip install customtkinter")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────
#  COLOUR PALETTE
# ─────────────────────────────────────────────────────────────
BG        = "#0B0E13"
SURFACE   = "#111520"
PANEL     = "#161C27"
BORDER    = "#1F2A38"
BORDER2   = "#2A3A50"
TEXT      = "#C5D4E8"
MUTED     = "#4A6078"
DIM       = "#283848"
ACCENT    = "#00D4AA"     # teal  – LIFE
BLUE      = "#3D8EFF"     # blue  – MOVE
AMBER     = "#FFB830"     # amber – CONFIRM
RED       = "#FF4455"     # red   – ERROR
PURPLE    = "#9966FF"     # purple – UNKNOWN
WHITE     = "#EEF4FF"
ENTRY_BG  = "#0B0E13"

TYPE_COLORS = {
    "LI": ACCENT,
    "MO": BLUE,
    "CF": AMBER,
    "ER": RED,
}
TYPE_LABELS = {
    "LI": "LIFE",
    "MO": "MOVE",
    "CF": "CNFM",
    "ER": "ERROR",
}

# ─────────────────────────────────────────────────────────────
#  TELEGRAM CODEC
# ─────────────────────────────────────────────────────────────
TELEGRAM_LEN = 128

def build_telegram(ttype: str, subtype: str, src: str, dst: str,
                   seq: int, data: str = "") -> bytes:
    """Encode a fixed-128-byte ASCII telegram."""
    body = (
        ttype[:2].upper().ljust(2)
        + str(subtype).zfill(2)[:2]
        + src[:8].ljust(8)
        + dst[:8].ljust(8)
        + str(seq % 1000000).zfill(6)
        + data[:102].ljust(102)
    )
    if len(body) != TELEGRAM_LEN:
        raise RuntimeError(
            f"build_telegram: body length {len(body)} != {TELEGRAM_LEN}. "
            "Check field widths."
        )
    # FIX 9 — encode safely; non-ASCII characters are replaced with '?'
    return body.encode("ascii", errors="replace")


def parse_telegram(raw: bytes) -> dict | None:
    """Decode a 128-byte telegram into a dict."""
    try:
        s = raw[:TELEGRAM_LEN].decode("ascii", errors="replace")
        ttype   = s[0:2].strip()
        subtype = s[2:4].strip()
        src     = s[4:12].strip()
        dst     = s[12:20].strip()
        seq     = s[20:26].strip()
        data    = s[26:128]

        parsed: dict = {
            "type":    ttype,
            "subtype": subtype,
            "src":     src,
            "dst":     dst,
            "seq":     seq,
            "data":    data,
            "raw":     s,
        }

        if ttype == "MO":
            parsed["tu"]      = data[0:20].strip()
            parsed["src_bin"] = data[20:40].strip()
            parsed["dst_bin"] = data[40:60].strip()
            parsed["priority"]= data[60:62].strip()
        elif ttype == "CF":
            parsed["tu"]      = data[0:20].strip()
            parsed["bin"]     = data[20:40].strip()
            parsed["status"]  = data[40:44].strip()
            parsed["ts"]      = data[44:58].strip()
        elif ttype == "ER":
            parsed["errcode"] = data[0:4].strip()
            parsed["errmsg"]  = data[4:102].strip()

        return parsed
    except Exception as exc:
        # Log the parse failure so it is visible during debugging
        import traceback
        print(f"[WARN] parse_telegram failed: {exc}\n  raw={raw[:32]!r}",
              flush=True)
        return None


def build_life(src: str, dst: str, seq: int, pong: bool = False) -> bytes:
    data = "PONG" if pong else "PING"
    return build_telegram("LI", "00", src, dst, seq, data.ljust(102))


def build_confirm(src: str, dst: str, seq: int,
                  tu: str, bin_: str, status: str = "DONE") -> bytes:
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    data = tu[:20].ljust(20) + bin_[:20].ljust(20) + status[:4].ljust(4) + ts.ljust(14)
    return build_telegram("CF", "00", src, dst, seq, data.ljust(102))


def build_error(src: str, dst: str, seq: int,
                code: str, msg: str) -> bytes:
    data = code[:4].ljust(4) + msg[:98].ljust(98)
    return build_telegram("ER", "00", src, dst, seq, data)


# ─────────────────────────────────────────────────────────────
#  TCP CONNECTION THREAD
# ─────────────────────────────────────────────────────────────
class TCPConnection(threading.Thread):
    """
    Manages the TCP socket to SAP EWM.
    Runs in its own thread; puts events into `event_q`.
    Sends bytes from `send_q`.
    """

    def __init__(self, host: str, port: int,
                 event_q: queue.Queue, send_q: queue.Queue):
        super().__init__(daemon=True)
        self.host    = host
        self.port    = port
        self.event_q = event_q
        self.send_q  = send_q
        self._stop   = threading.Event()
        self.sock: socket.socket | None = None

    def stop(self):
        self._stop.set()
        if self.sock:
            try: self.sock.shutdown(socket.SHUT_RDWR)
            except Exception: pass

    def run(self):
        self.event_q.put(("status", "connecting"))
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5)
            self.sock.connect((self.host, self.port))
            self.sock.settimeout(0.5)
            self.event_q.put(("status", "connected"))

            MAX_BUF = TELEGRAM_LEN * 256   # FIX 3 — cap at 256 queued telegrams (~32 KB)
            buf = b""
            while not self._stop.is_set():
                # send queued outgoing
                try:
                    while True:
                        raw = self.send_q.get_nowait()
                        self.sock.sendall(raw)
                        self.event_q.put(("sent", raw))
                except queue.Empty:
                    pass

                # receive incoming
                try:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    # FIX 3 — discard excess bytes to prevent OOM
                    if len(buf) > MAX_BUF:
                        discard = len(buf) - MAX_BUF
                        buf = buf[discard:]
                        self.event_q.put(("error", f"Receive buffer overflow — discarded {discard} bytes"))
                    while len(buf) >= TELEGRAM_LEN:
                        self.event_q.put(("recv", buf[:TELEGRAM_LEN]))
                        buf = buf[TELEGRAM_LEN:]
                except socket.timeout:
                    pass
                except Exception as e:
                    self.event_q.put(("error", str(e)))
                    break

        except Exception as e:
            self.event_q.put(("status", "error"))
            self.event_q.put(("error", str(e)))
            return
        finally:
            # FIX 10 — always close socket, prevent file descriptor leak
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None

        self.event_q.put(("status", "disconnected"))


# ─────────────────────────────────────────────────────────────
#  MAIN APPLICATION
# ─────────────────────────────────────────────────────────────
class PLCSimApp(ctk.CTk):

    def __init__(self):
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.title("PLC·SIM — SAP EWM MFS Device Simulator")
        self.geometry("1280x800")
        self.minsize(900, 600)
        self.configure(fg_color=BG)

        # fonts
        self.f_mono_sm  = CTkFont(family="JetBrains Mono", size=11)
        self.f_mono_md  = CTkFont(family="JetBrains Mono", size=12)
        self.f_mono_lg  = CTkFont(family="JetBrains Mono", size=13)
        self.f_head     = CTkFont(family="Syne",           size=13, weight="bold")
        self.f_label    = CTkFont(family="JetBrains Mono", size=10)
        self.f_status   = CTkFont(family="JetBrains Mono", size=11)
        self.f_big      = CTkFont(family="Syne",           size=18, weight="bold")

        # fallback fonts if custom not installed
        try:
            self.f_mono_sm.configure()
        except Exception:
            self.f_mono_sm = CTkFont(size=11)
            self.f_mono_md = CTkFont(size=12)
            self.f_mono_lg = CTkFont(size=13)
            self.f_head    = CTkFont(size=13, weight="bold")
            self.f_label   = CTkFont(size=10)
            self.f_status  = CTkFont(size=11)
            self.f_big     = CTkFont(size=18, weight="bold")

        # state
        self.tcp_thread:   TCPConnection | None = None
        self.event_q:      queue.Queue   = queue.Queue()
        self.send_q:       queue.Queue   = queue.Queue()
        self.connected:    bool          = False
        self.messages:     list          = []
        self.selected_idx: int | None    = None
        self.seq:          int           = 0
        self.rx_count:     int           = 0
        self.tx_count:     int           = 0

        # auto-life
        self.auto_life:      bool = False
        self.life_interval:  int  = 10
        self.auto_life_job: str | None = None

        # auto-confirm
        self.auto_confirm: bool = True

        self._build_ui()
        self._poll_events()

    # ── sequence helper ───────────────────────────────────────
    def next_seq(self) -> int:
        self.seq = (self.seq + 1) % 1000000
        return self.seq

    def device_id(self) -> str:
        return self.ent_device.get().strip() or "PLC-SIM"

    def sap_id(self) -> str:
        return self.ent_sap.get().strip() or "EWM-MFS"

    # ─────────────────────────────────────────────────────────
    #  UI BUILD
    # ─────────────────────────────────────────────────────────
    def _build_ui(self):
        # accent bar at top
        accent_bar = ctk.CTkFrame(self, height=2, fg_color=ACCENT, corner_radius=0)
        accent_bar.pack(fill="x", side="top")

        self._build_header()
        self._build_toolbar()

        # main area
        main = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=0)
        main.rowconfigure(0, weight=1)

        self._build_log_panel(main)
        self._build_detail_panel(main)
        self._build_statusbar()

    # ── HEADER ───────────────────────────────────────────────
    def _build_header(self):
        hdr = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0, height=54)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        inner = ctk.CTkFrame(hdr, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=20)

        # logo
        logo_frame = ctk.CTkFrame(inner, fg_color="transparent")
        logo_frame.pack(side="left", pady=12)

        self.conn_dot = ctk.CTkLabel(
            logo_frame, text="●", font=CTkFont(size=10),
            text_color=MUTED, width=14
        )
        self.conn_dot.pack(side="left")

        ctk.CTkLabel(
            logo_frame, text="PLC", font=self.f_big, text_color=WHITE
        ).pack(side="left", padx=(6, 0))
        ctk.CTkLabel(
            logo_frame, text="·SIM", font=self.f_big, text_color=ACCENT
        ).pack(side="left")

        # subtitle
        ctk.CTkLabel(
            inner,
            text="SAP EWM MFS Device Simulator",
            font=self.f_label, text_color=MUTED
        ).pack(side="left", padx=16, pady=12)

        # status label (right)
        self.lbl_status = ctk.CTkLabel(
            inner, text="OFFLINE",
            font=self.f_status, text_color=MUTED
        )
        self.lbl_status.pack(side="right", pady=12)

        # bottom border gradient (fake with frame)
        border = ctk.CTkFrame(hdr, height=1, fg_color=BORDER2, corner_radius=0)
        border.pack(fill="x", side="bottom")

    # ── TOOLBAR ───────────────────────────────────────────────
    def _build_toolbar(self):
        tb = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0, height=48)
        tb.pack(fill="x")
        tb.pack_propagate(False)

        inner = ctk.CTkFrame(tb, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=16)

        def sep():
            ctk.CTkFrame(inner, width=1, fg_color=BORDER2, corner_radius=0
                         ).pack(side="left", fill="y", padx=8, pady=10)

        def lbl(text):
            ctk.CTkLabel(inner, text=text, font=self.f_label,
                         text_color=MUTED).pack(side="left", padx=(0, 4))

        # Connect block
        lbl("HOST")
        self.ent_host = ctk.CTkEntry(
            inner, width=140, height=28,
            font=self.f_mono_sm, fg_color=ENTRY_BG,
            border_color=BORDER2, text_color=TEXT,
            placeholder_text="127.0.0.1"
        )
        self.ent_host.insert(0, "127.0.0.1")
        self.ent_host.pack(side="left", pady=10)

        lbl("  PORT")
        self.ent_port = ctk.CTkEntry(
            inner, width=65, height=28,
            font=self.f_mono_sm, fg_color=ENTRY_BG,
            border_color=BORDER2, text_color=TEXT,
            placeholder_text="5000"
        )
        self.ent_port.insert(0, "5000")
        self.ent_port.pack(side="left", pady=10)

        sep()

        lbl("DEVICE ID")
        self.ent_device = ctk.CTkEntry(
            inner, width=90, height=28,
            font=self.f_mono_sm, fg_color=ENTRY_BG,
            border_color=BORDER2, text_color=TEXT,
            placeholder_text="PLC-SIM"
        )
        self.ent_device.insert(0, "PLC-SIM")
        self.ent_device.pack(side="left", pady=10)

        lbl("  SAP ID")
        self.ent_sap = ctk.CTkEntry(
            inner, width=90, height=28,
            font=self.f_mono_sm, fg_color=ENTRY_BG,
            border_color=BORDER2, text_color=TEXT,
            placeholder_text="EWM-MFS"
        )
        self.ent_sap.insert(0, "EWM-MFS")
        self.ent_sap.pack(side="left", pady=10)

        sep()

        self.btn_connect = ctk.CTkButton(
            inner, text="▶  Connect", width=110, height=28,
            font=self.f_mono_sm,
            fg_color=ACCENT, text_color="#000", hover_color="#00b890",
            command=self._connect
        )
        self.btn_connect.pack(side="left", pady=10)

        self.btn_disconnect = ctk.CTkButton(
            inner, text="■  Disconnect", width=110, height=28,
            font=self.f_mono_sm,
            fg_color=RED, text_color=WHITE, hover_color="#cc2233",
            command=self._disconnect, state="disabled"
        )
        self.btn_disconnect.pack(side="left", padx=6, pady=10)

        sep()

        # auto-life toggle
        self.btn_autolife = ctk.CTkButton(
            inner, text="LIFE: OFF", width=90, height=28,
            font=self.f_label,
            fg_color=DIM, text_color=MUTED, hover_color=BORDER2,
            command=self._toggle_autolife
        )
        self.btn_autolife.pack(side="left", pady=10)

        lbl("  every")
        self.ent_life_int = ctk.CTkEntry(
            inner, width=42, height=28,
            font=self.f_mono_sm, fg_color=ENTRY_BG,
            border_color=BORDER2, text_color=TEXT
        )
        self.ent_life_int.insert(0, "10")
        self.ent_life_int.pack(side="left", pady=10)
        lbl("s")

        sep()

        self.btn_autoconfirm = ctk.CTkButton(
            inner, text="AUTO-CNFM: ON", width=120, height=28,
            font=self.f_label,
            fg_color=BLUE, text_color=WHITE, hover_color="#2266cc",
            command=self._toggle_autoconfirm
        )
        self.btn_autoconfirm.pack(side="left", pady=10)

        sep()

        ctk.CTkButton(
            inner, text="Clear", width=60, height=28,
            font=self.f_label,
            fg_color=SURFACE, text_color=MUTED,
            border_color=BORDER2, border_width=1,
            hover_color=BORDER, command=self._clear_log
        ).pack(side="left", pady=10)

        ctk.CTkButton(
            inner, text="Export", width=60, height=28,
            font=self.f_label,
            fg_color=SURFACE, text_color=MUTED,
            border_color=BORDER2, border_width=1,
            hover_color=BORDER, command=self._export_log
        ).pack(side="left", padx=4, pady=10)

        # bottom border
        ctk.CTkFrame(tb, height=1, fg_color=BORDER, corner_radius=0
                     ).pack(fill="x", side="bottom")

    # ── LOG PANEL ─────────────────────────────────────────────
    def _build_log_panel(self, parent):
        frame = ctk.CTkFrame(parent, fg_color=BG, corner_radius=0)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)

        # column header
        hdr = ctk.CTkFrame(frame, fg_color=PANEL, corner_radius=0, height=28)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.columnconfigure((0,1,2,3,4,5), weight=0)

        cols = [
            ("Time",    100, "w"),
            ("Dir",      42, "center"),
            ("Source",   90, "w"),
            ("Type",     60, "center"),
            ("Payload", 999, "w"),
            ("HS",       42, "center"),
        ]
        for i, (name, w, anc) in enumerate(cols):
            ctk.CTkLabel(
                hdr, text=name, font=self.f_label,
                text_color=MUTED, anchor=anc
            ).grid(row=0, column=i, padx=(12 if i==0 else 6, 6),
                   sticky="w", pady=4)

        # scrollable log
        self.log_frame = ctk.CTkScrollableFrame(
            frame, fg_color=BG, corner_radius=0,
            scrollbar_button_color=DIM,
            scrollbar_button_hover_color=BORDER2
        )
        self.log_frame.grid(row=1, column=0, sticky="nsew")
        self.log_frame.columnconfigure(0, weight=1)

        self._render_log_empty()

    def _render_log_empty(self):
        for w in self.log_frame.winfo_children():
            w.destroy()
        ctk.CTkLabel(
            self.log_frame,
            text="\n\n◎\n\nConnect to SAP EWM to begin receiving telegrams",
            font=self.f_label, text_color=DIM
        ).pack(expand=True, pady=60)

    def _render_log(self):
        for w in self.log_frame.winfo_children():
            w.destroy()

        if not self.messages:
            self._render_log_empty()
            return

        for i, m in enumerate(self.messages):
            self._render_log_row(i, m)

        self._scroll_log_bottom()

    def _render_log_row(self, i: int, m: dict):
        is_sel = (self.selected_idx == i)
        row_bg = "#152028" if is_sel else "transparent"
        tcolor = TYPE_COLORS.get(m["type"], PURPLE)
        dircolor = BLUE if m["dir"] == "RX" else AMBER

        row = ctk.CTkFrame(
            self.log_frame, fg_color=row_bg, corner_radius=4,
            height=26, cursor="hand2"
        )
        row.pack(fill="x", padx=4, pady=1)
        row.bind("<Button-1>", lambda e, idx=i: self._select_row(idx))

        def add(text, color, width, anchor="w"):
            lbl = ctk.CTkLabel(
                row, text=text, font=self.f_mono_sm,
                text_color=color, anchor=anchor, width=width
            )
            lbl.pack(side="left", padx=5)
            lbl.bind("<Button-1>", lambda e, idx=i: self._select_row(idx))

        add(m["time"],                      MUTED,   100)
        add(m["dir"],                       dircolor,  36)
        add(m["src"][:10],                  TEXT,      90)
        add(TYPE_LABELS.get(m["type"], m["type"]), tcolor, 58)
        add(m["payload_short"],             DIM,       0 )
        add(m.get("hs", "—"),              AMBER if m.get("hs") else DIM, 40, "center")

    def _scroll_log_bottom(self):
        try:
            self.log_frame._parent_canvas.yview_moveto(1.0)
        except Exception:
            pass

    def _select_row(self, idx: int):
        self.selected_idx = idx
        self._render_log()
        self._render_detail(self.messages[idx])

    # ── DETAIL PANEL ──────────────────────────────────────────
    def _build_detail_panel(self, parent):
        frame = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=0, width=340)
        frame.grid(row=0, column=1, sticky="nsew")
        frame.grid_propagate(False)
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)

        # header
        hdr = ctk.CTkFrame(frame, fg_color=PANEL, corner_radius=0, height=36)
        hdr.grid(row=0, column=0, sticky="ew")

        ctk.CTkLabel(
            hdr, text="TELEGRAM DETAIL",
            font=self.f_label, text_color=MUTED
        ).pack(side="left", padx=16, pady=8)

        self.lbl_detail_id = ctk.CTkLabel(
            hdr, text="", font=self.f_label, text_color=ACCENT
        )
        self.lbl_detail_id.pack(side="right", padx=16)

        # scrollable detail body
        self.detail_scroll = ctk.CTkScrollableFrame(
            frame, fg_color=SURFACE, corner_radius=0,
            scrollbar_button_color=DIM,
            scrollbar_button_hover_color=BORDER2
        )
        self.detail_scroll.grid(row=1, column=0, sticky="nsew")

        # send / compose panel (bottom)
        self._build_send_panel(frame)

        self._render_detail_empty()

    def _render_detail_empty(self):
        for w in self.detail_scroll.winfo_children():
            w.destroy()
        ctk.CTkLabel(
            self.detail_scroll,
            text="\n◎\n\nSelect a telegram\nto inspect its structure",
            font=self.f_label, text_color=DIM
        ).pack(pady=40)

    def _render_detail(self, m: dict):
        for w in self.detail_scroll.winfo_children():
            w.destroy()

        tcolor = TYPE_COLORS.get(m["type"], PURPLE)

        def section(title):
            ctk.CTkLabel(
                self.detail_scroll, text=title,
                font=self.f_label, text_color=MUTED, anchor="w"
            ).pack(fill="x", padx=14, pady=(12, 4))
            ctk.CTkFrame(
                self.detail_scroll, height=1, fg_color=BORDER, corner_radius=0
            ).pack(fill="x", padx=14)

        def field(label, value, vcolor=TEXT):
            row = ctk.CTkFrame(self.detail_scroll, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=2)
            ctk.CTkLabel(
                row, text=label, font=self.f_label,
                text_color=MUTED, width=100, anchor="w"
            ).pack(side="left")
            ctk.CTkLabel(
                row, text=str(value), font=self.f_mono_sm,
                text_color=vcolor, anchor="w"
            ).pack(side="left", fill="x", expand=True)

        # type badge
        badge_f = ctk.CTkFrame(self.detail_scroll, fg_color="transparent")
        badge_f.pack(fill="x", padx=14, pady=(12, 6))
        ctk.CTkLabel(
            badge_f,
            text=f"  {TYPE_LABELS.get(m['type'], m['type'])}  ",
            font=CTkFont(size=11, weight="bold"),
            text_color="#000", fg_color=tcolor, corner_radius=4
        ).pack(side="left")

        section("HEADER")
        field("Timestamp",  m["time"])
        field("Direction",  m["dir"],    BLUE if m["dir"]=="RX" else AMBER)
        field("Type",       TYPE_LABELS.get(m["type"], m["type"]), tcolor)
        field("SubType",    m.get("subtype",""))
        field("Source",     m["src"])
        field("Destination",m["dst"])
        field("Sequence",   m["seq"])
        field("Handshake",  m.get("hs","—"), AMBER if m.get("hs") else MUTED)

        section("FIELDS")
        ttype = m["type"]
        if ttype == "MO":
            field("Transfer Unit", m.get("tu",""), ACCENT)
            field("Source Bin",    m.get("src_bin",""))
            field("Dest Bin",      m.get("dst_bin",""), BLUE)
            field("Priority",      m.get("priority",""))
        elif ttype == "CF":
            field("Transfer Unit", m.get("tu",""), ACCENT)
            field("Bin",           m.get("bin",""))
            field("Status",        m.get("status",""), ACCENT)
            field("Timestamp",     m.get("ts",""))
        elif ttype == "ER":
            field("Error Code",    m.get("errcode",""), RED)
            field("Message",       m.get("errmsg",""), RED)
        elif ttype == "LI":
            field("Payload",       m.get("data","").strip() or "—")

        section("HEX DUMP")
        raw = m.get("raw", "")
        hex_str = " ".join(f"{ord(c):02X}" for c in raw[:64])
        txt = ctk.CTkTextbox(
            self.detail_scroll, height=90,
            font=CTkFont(family="JetBrains Mono", size=10),
            fg_color=ENTRY_BG, text_color=MUTED,
            border_color=BORDER, border_width=1,
            corner_radius=4, state="normal"
        )
        txt.pack(fill="x", padx=14, pady=6)
        txt.insert("0.0", hex_str)
        txt.configure(state="disabled")

        # quick action buttons (only for RX MOVE)
        if m["dir"] == "RX" and m["type"] == "MO":
            section("ACTIONS")
            ctk.CTkButton(
                self.detail_scroll,
                text="▶  Send CONFIRMATION",
                font=self.f_mono_sm,
                fg_color=AMBER, text_color="#000", hover_color="#cc9020",
                height=32, corner_radius=4,
                command=lambda: self._send_confirm_for(m)
            ).pack(fill="x", padx=14, pady=4)
            ctk.CTkButton(
                self.detail_scroll,
                text="✕  Send ERROR",
                font=self.f_mono_sm,
                fg_color=SURFACE, text_color=RED,
                border_color=RED, border_width=1,
                hover_color=BORDER, height=32, corner_radius=4,
                command=lambda: self._send_error_for(m)
            ).pack(fill="x", padx=14, pady=(0, 12))

        self.lbl_detail_id.configure(text=f"#{m['idx']:04d}")

    # ── SEND PANEL ────────────────────────────────────────────
    def _build_send_panel(self, parent):
        pnl = ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=0)
        pnl.grid(row=2, column=0, sticky="ew")

        # section label
        ctk.CTkLabel(
            pnl, text="MANUAL SEND",
            font=self.f_label, text_color=MUTED
        ).pack(anchor="w", padx=14, pady=(10, 4))

        ctk.CTkFrame(pnl, height=1, fg_color=BORDER, corner_radius=0
                     ).pack(fill="x", padx=14)

        # type + hs row
        row1 = ctk.CTkFrame(pnl, fg_color="transparent")
        row1.pack(fill="x", padx=14, pady=6)

        ctk.CTkLabel(row1, text="Type", font=self.f_label, text_color=MUTED
                     ).pack(side="left")
        self.cmb_type = ctk.CTkComboBox(
            row1, values=["LI – LIFE", "MO – MOVE", "CF – CNFM", "ER – ERROR"],
            width=130, height=28, font=self.f_mono_sm,
            fg_color=ENTRY_BG, border_color=BORDER2,
            button_color=BORDER2, dropdown_fg_color=PANEL,
            text_color=TEXT, dropdown_text_color=TEXT,
            command=self._on_type_select
        )
        self.cmb_type.set("LI – LIFE")
        self.cmb_type.pack(side="left", padx=8)

        ctk.CTkLabel(row1, text="HS", font=self.f_label, text_color=MUTED
                     ).pack(side="left")
        self.cmb_hs = ctk.CTkComboBox(
            row1, values=["—", "REQ", "ACK"],
            width=70, height=28, font=self.f_mono_sm,
            fg_color=ENTRY_BG, border_color=BORDER2,
            button_color=BORDER2, dropdown_fg_color=PANEL,
            text_color=TEXT, dropdown_text_color=TEXT,
        )
        self.cmb_hs.set("—")
        self.cmb_hs.pack(side="left", padx=8)

        # data row
        row2 = ctk.CTkFrame(pnl, fg_color="transparent")
        row2.pack(fill="x", padx=14, pady=(0, 4))
        ctk.CTkLabel(row2, text="Data", font=self.f_label, text_color=MUTED
                     ).pack(side="left")
        self.ent_data = ctk.CTkEntry(
            row2, height=28, font=self.f_mono_sm,
            fg_color=ENTRY_BG, border_color=BORDER2, text_color=TEXT,
            placeholder_text="TU0001              BIN-01              BIN-99"
        )
        self.ent_data.pack(side="left", fill="x", expand=True, padx=(8, 0))

        # send button
        ctk.CTkButton(
            pnl, text="Send Telegram  ▶",
            font=self.f_mono_sm,
            fg_color=ACCENT, text_color="#000", hover_color="#00b890",
            height=32, corner_radius=4,
            command=self._manual_send
        ).pack(fill="x", padx=14, pady=(4, 12))

        # bottom border
        ctk.CTkFrame(pnl, height=1, fg_color=BORDER, corner_radius=0
                     ).pack(fill="x", side="bottom")

    # ── STATUS BAR ────────────────────────────────────────────
    def _build_statusbar(self):
        sb = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0, height=28)
        sb.pack(fill="x", side="bottom")
        sb.pack_propagate(False)

        ctk.CTkFrame(sb, height=1, fg_color=BORDER, corner_radius=0
                     ).pack(fill="x", side="top")

        inner = ctk.CTkFrame(sb, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=16)

        def item(label):
            f = ctk.CTkFrame(inner, fg_color="transparent")
            f.pack(side="left", padx=10)
            ctk.CTkLabel(f, text=label, font=self.f_label, text_color=MUTED
                         ).pack(side="left")
            v = ctk.CTkLabel(f, text="—", font=self.f_label, text_color=TEXT)
            v.pack(side="left", padx=4)
            return v

        self.sb_host    = item("Host")
        self.sb_rx      = item("RX")
        self.sb_tx      = item("TX")
        self.sb_life    = item("LIFE")
        self.sb_msgs    = item("Messages")

        ctk.CTkLabel(
            inner, text="PLC·SIM v2.0",
            font=self.f_label, text_color=DIM
        ).pack(side="right")

    # ─────────────────────────────────────────────────────────
    #  EVENT POLLING
    # ─────────────────────────────────────────────────────────
    def _poll_events(self):
        try:
            while True:
                ev_type, data = self.event_q.get_nowait()
                self._handle_event(ev_type, data)
        except queue.Empty:
            pass
        self.after(80, self._poll_events)

    def _handle_event(self, ev_type: str, data):
        if ev_type == "status":
            self._on_status(data)
        elif ev_type == "recv":
            self._on_recv(data)
        elif ev_type == "sent":
            self._on_sent(data)
        elif ev_type == "error":
            self._log_system(f"ERROR: {data}", RED)

    def _on_status(self, status: str):
        colors = {
            "connecting":    (AMBER,  "CONNECTING…"),
            "connected":     (ACCENT, "CONNECTED"),
            "disconnected":  (MUTED,  "OFFLINE"),
            "error":         (RED,    "CONNECTION ERROR"),
        }
        color, label = colors.get(status, (MUTED, status.upper()))
        self.conn_dot.configure(text_color=color)
        self.lbl_status.configure(text=label, text_color=color)

        if status == "connected":
            self.connected = True
            self.btn_connect.configure(state="disabled")
            self.btn_disconnect.configure(state="normal")
            host = self.ent_host.get()
            port = self.ent_port.get()
            self.sb_host.configure(text=f"{host}:{port}", text_color=ACCENT)
            self._log_system(f"Connected to {host}:{port}", ACCENT)
        elif status in ("disconnected", "error"):
            self.connected = False
            self.btn_connect.configure(state="normal")
            self.btn_disconnect.configure(state="disabled")
            self._stop_autolife()
            self._log_system("Disconnected", MUTED)

    def _on_recv(self, raw: bytes):
        m = parse_telegram(raw)
        if not m:
            return
        m["dir"]  = "RX"
        m["time"] = datetime.datetime.now().strftime("%H:%M:%S.%f")[:12]
        m["hs"]   = ""
        m["idx"]  = len(self.messages) + 1
        m["payload_short"] = m.get("data", "").strip()[:60]
        # FIX 4 — cap log to 5000 entries (drop oldest) to prevent OOM
        MAX_LOG = 5000
        if len(self.messages) >= MAX_LOG:
            self.messages = self.messages[-(MAX_LOG - 1):]
            if self.selected_idx is not None:
                self.selected_idx = max(0, self.selected_idx - 1)
        self.messages.append(m)
        self.rx_count += 1
        self.sb_rx.configure(text=str(self.rx_count), text_color=ACCENT)
        self.sb_msgs.configure(text=str(len(self.messages)), text_color=TEXT)
        self._render_log()

        # auto-confirm MOVE ORDER
        if m["type"] == "MO" and self.auto_confirm:
            self.after(500, lambda: self._send_confirm_for(m))

        # FIX 5 — rate-limit auto-PONG: at most one per second regardless of burst
        if m["type"] == "LI":
            now_ts = time.monotonic()
            if not hasattr(self, "_last_pong_ts") or now_ts - self._last_pong_ts >= 1.0:
                self._last_pong_ts = now_ts
                self.after(200, self._send_life_pong)

    def _on_sent(self, raw: bytes):
        m = parse_telegram(raw)
        if not m:
            return
        m["dir"]  = "TX"
        m["time"] = datetime.datetime.now().strftime("%H:%M:%S.%f")[:12]
        m["hs"]   = ""
        m["idx"]  = len(self.messages) + 1
        m["payload_short"] = m.get("data", "").strip()[:60]
        self.messages.append(m)
        self.tx_count += 1
        self.sb_tx.configure(text=str(self.tx_count), text_color=AMBER)
        self.sb_msgs.configure(text=str(len(self.messages)), text_color=TEXT)
        self._render_log()

    # ─────────────────────────────────────────────────────────
    #  ACTIONS
    # ─────────────────────────────────────────────────────────
    def _connect(self):
        # FIX 8 — validate host is non-empty
        host = self.ent_host.get().strip()
        if not host:
            self._log_system("Host cannot be empty", RED)
            return

        # FIX 6 — validate port is in legal range 1–65535
        try:
            port = int(self.ent_port.get().strip())
            if not (1 <= port <= 65535):
                raise ValueError("out of range")
        except ValueError:
            self._log_system("Port must be an integer between 1 and 65535", RED)
            return

        # FIX 11 — stop any existing thread before starting a new one
        if self.tcp_thread and self.tcp_thread.is_alive():
            self.tcp_thread.stop()
            self.tcp_thread.join(timeout=3)

        # Drain stale events and send queue from previous session
        while not self.event_q.empty():
            try: self.event_q.get_nowait()
            except queue.Empty: break
        while not self.send_q.empty():
            try: self.send_q.get_nowait()
            except queue.Empty: break

        self.tcp_thread = TCPConnection(host, port, self.event_q, self.send_q)
        self.tcp_thread.start()

    def _disconnect(self):
        if self.tcp_thread:
            self.tcp_thread.stop()
        self._stop_autolife()

    def _send(self, raw: bytes):
        if not self.connected:
            self._log_system("Not connected", RED)
            return
        self.send_q.put(raw)

    def _send_life_ping(self):
        raw = build_life(self.device_id(), self.sap_id(), self.next_seq(), pong=False)
        self._send(raw)
        self.sb_life.configure(
            text=datetime.datetime.now().strftime("%H:%M:%S"), text_color=ACCENT
        )

    def _send_life_pong(self):
        raw = build_life(self.device_id(), self.sap_id(), self.next_seq(), pong=True)
        self._send(raw)

    def _send_confirm_for(self, m: dict):
        raw = build_confirm(
            self.device_id(), self.sap_id(), self.next_seq(),
            m.get("tu", "UNKNOWN"), m.get("dst_bin", "?"), "DONE"
        )
        self._send(raw)

    def _send_error_for(self, m: dict):
        raw = build_error(
            self.device_id(), self.sap_id(), self.next_seq(),
            "E001", f"Manual error for TU {m.get('tu','?')}"
        )
        self._send(raw)

    def _manual_send(self):
        type_map = {"LI – LIFE":"LI", "MO – MOVE":"MO", "CF – CNFM":"CF", "ER – ERROR":"ER"}
        raw_type = self.cmb_type.get()
        ttype = type_map.get(raw_type, "LI")
        data  = self.ent_data.get().ljust(102)[:102]
        raw   = build_telegram(ttype, "00", self.device_id(), self.sap_id(),
                               self.next_seq(), data)
        self._send(raw)

    def _on_type_select(self, val):
        hints = {
            "LI – LIFE": "PING or PONG",
            "MO – MOVE": "TU0001              BIN-01              BIN-99",
            "CF – CNFM": "TU0001              BIN-99              DONE",
            "ER – ERROR": "E001COMM_TIMEOUT                                   ",
        }
        self.ent_data.configure(placeholder_text=hints.get(val, ""))

    def _toggle_autolife(self):
        self.auto_life = not self.auto_life
        if self.auto_life:
            self.btn_autolife.configure(
                text="LIFE: ON", fg_color=ACCENT, text_color="#000"
            )
            self._schedule_autolife()
        else:
            self.btn_autolife.configure(
                text="LIFE: OFF", fg_color=DIM, text_color=MUTED
            )
            self._stop_autolife()

    def _schedule_autolife(self):
        if not self.auto_life:
            return
        if self.connected:
            self._send_life_ping()
        try:
            # FIX 7 — enforce minimum 1 s to prevent tight CPU/network loop
            interval = max(1, int(self.ent_life_int.get())) * 1000
        except ValueError:
            interval = 10000
        self.auto_life_job = self.after(interval, self._schedule_autolife)

    def _stop_autolife(self):
        if self.auto_life_job:
            self.after_cancel(self.auto_life_job)
            self.auto_life_job = None

    def _toggle_autoconfirm(self):
        self.auto_confirm = not self.auto_confirm
        if self.auto_confirm:
            self.btn_autoconfirm.configure(
                text="AUTO-CNFM: ON", fg_color=BLUE, text_color=WHITE
            )
        else:
            self.btn_autoconfirm.configure(
                text="AUTO-CNFM: OFF", fg_color=DIM, text_color=MUTED
            )

    def _clear_log(self):
        self.messages.clear()
        self.selected_idx = None
        self.rx_count = 0
        self.tx_count = 0
        self.sb_rx.configure(text="0")
        self.sb_tx.configure(text="0")
        self.sb_msgs.configure(text="0")
        self._render_log()
        self._render_detail_empty()
        self.lbl_detail_id.configure(text="")

    def _export_log(self):
        if not self.messages:
            return

        def _safe(val: str) -> str:
            # FIX 12 — strip tab/newline chars from SAP-sourced data to prevent TSV injection
            return str(val).replace("\t", " ").replace("\n", " ").replace("\r", "")

        path = os.path.join(os.path.expanduser("~"),
                            f"plcsim-export-{int(time.time())}.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("TIME\t\t\tDIR\tSRC\t\tDST\t\tTYPE\tSEQ\tDATA\n")
                f.write("─" * 100 + "\n")
                for m in self.messages:
                    f.write(
                        f"{_safe(m['time'])}\t{_safe(m['dir'])}\t{_safe(m.get('src','')):<8}\t"
                        f"{_safe(m.get('dst','')):<8}\t{_safe(m['type'])}\t{_safe(m['seq'])}\t"
                        f"{_safe(m.get('data','').strip())}\n"
                    )
            self._log_system(f"Exported → {path}", ACCENT)
        except OSError as exc:
            self._log_system(f"Export failed: {exc}", RED)

    def _log_system(self, msg: str, color=MUTED):
        # Synthetic system message for the log
        m = {
            "dir":           "SYS",
            "time":          datetime.datetime.now().strftime("%H:%M:%S.%f")[:12],
            "src":           "SYSTEM",
            "dst":           "",
            "type":          "SYS",
            "subtype":       "",
            "seq":           "—",
            "hs":            "",
            "data":          msg,
            "raw":           "",
            "idx":           len(self.messages) + 1,
            "payload_short": msg,
        }
        self.messages.append(m)
        self.sb_msgs.configure(text=str(len(self.messages)))
        self._render_log()


# ─────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = PLCSimApp()
    app.mainloop()
