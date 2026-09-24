"""Parley desktop application.

Blank-slate desktop UI built around the production fresh-chat lifecycle.
The UI owns presentation only; runtime lifecycle lives in desktop_controller.py,
startup orchestration lives in workflows.py, and relay sequencing lives in parley.relay.
"""

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from . import core
from .desktop_controller import DesktopController
from .participants import eligible_chatgpt_tabs
from .relay.engine import (
    TEST_A_REPLY_PREFIX,
    TEST_A_REPLY_SUFFIX,
    TEST_B_REPLY_PREFIX,
    TEST_B_REPLY_SUFFIX,
)

BG = "#0a0f1c"
PANEL = "#101827"
PANEL_ALT = "#151f31"
INPUT = "#0b1322"
BORDER = "#26344b"
TEXT = "#edf3fb"
MUTED = "#8e9db2"
ACCENT = "#7d9cff"
ACCENT_HOVER = "#9aafff"
A_COLOR = "#78c5ff"
B_COLOR = "#d3a6ff"
SUCCESS = "#5dd6a9"
WARN = "#f1c46b"
DANGER = "#ff8194"


def _display_reply(text, label):
    value = str(text or "").strip()
    if label == "A":
        prefix, suffix = TEST_A_REPLY_PREFIX, TEST_A_REPLY_SUFFIX
    else:
        prefix, suffix = TEST_B_REPLY_PREFIX, TEST_B_REPLY_SUFFIX
    if value.startswith(prefix):
        value = value[len(prefix):].lstrip()
    if value.endswith(suffix):
        value = value[:-len(suffix)].rstrip()
    return value


def _phase_from_status(status):
    if status.get("awaiting_extension"):
        return "Round complete"
    if status.get("control") == "paused":
        return "Paused"
    state = status.get("state")
    return {
        "IDLE": "Idle",
        "PREPARE": "Preparing relay",
        "READ_A": "Reading A",
        "TRANSFER_A_TO_B": "A → B",
        "TRANSFER_B_TO_A": "B → A",
        "COMPLETE": "Complete",
        "STOPPED": "Stopped",
        "ERROR": "Error",
    }.get(state, (state or "Idle").replace("_", " ").title())


class ParleyApp:
    POLL_MS = 250
    UI_DRAIN_MS = 60

    def __init__(self, root):
        self.root = root
        self.root.title("Parley")
        self.root.geometry("1280x820")
        self.root.minsize(1040, 700)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._ui_queue = queue.Queue()
        self.controller = DesktopController()
        self._diagnostic_lines = []
        self._diagnostic_window = None
        self._diagnostic_text = None
        self._closing = False
        self._mode = "idle"
        self._participant_tabs = []
        self._participant_tab_map = {}
        self._participant_source_vars = {}
        self._participant_tab_vars = {}
        self._participant_tab_boxes = {}

        self._build_style()
        self._build_ui()
        self._set_mode("idle")
        self._set_participant("A", "Waiting", MUTED)
        self._set_participant("B", "Waiting", MUTED)
        self._update_session_card(
            state="Ready",
            phase="New conversation",
            rounds="0 / 0",
            transfers="0",
        )

        self.root.after(self.UI_DRAIN_MS, self._drain_ui_queue)
        self.root.after(self.POLL_MS, self._poll)
        self._check_browser_async()

    def _build_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "Primary.TButton",
            font=("Segoe UI Semibold", 10),
            padding=(16, 10),
            background=ACCENT,
            foreground="#07101f",
            borderwidth=0,
        )
        style.map(
            "Primary.TButton",
            background=[("active", ACCENT_HOVER), ("disabled", "#344057")],
            foreground=[("disabled", "#77869a")],
        )
        style.configure(
            "Secondary.TButton",
            font=("Segoe UI", 10),
            padding=(12, 9),
            background=PANEL_ALT,
            foreground=TEXT,
            bordercolor=BORDER,
            borderwidth=1,
        )
        style.map(
            "Secondary.TButton",
            background=[("active", "#1c2940"), ("disabled", "#111a28")],
            foreground=[("disabled", "#58677b")],
        )
        style.configure(
            "Danger.TButton",
            font=("Segoe UI Semibold", 10),
            padding=(12, 9),
            background="#47212a",
            foreground="#ffd9df",
            borderwidth=0,
        )
        style.map(
            "Danger.TButton",
            background=[("active", "#622b37"), ("disabled", "#211820")],
            foreground=[("disabled", "#765b62")],
        )
        style.configure(
            "Parley.Horizontal.TProgressbar",
            troughcolor=INPUT,
            background=ACCENT,
            bordercolor=INPUT,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
        )

    def _build_ui(self):
        header = tk.Frame(self.root, bg=BG, height=72)
        header.pack(fill="x", padx=26, pady=(18, 8))
        header.pack_propagate(False)

        brand = tk.Frame(header, bg=BG)
        brand.pack(side="left", fill="y")
        tk.Label(
            brand,
            text="PARLEY",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 21),
        ).pack(anchor="w")
        tk.Label(
            brand,
            text="AI-to-AI conversation console",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(1, 0))

        self.diagnostics_button = ttk.Button(
            header,
            text="Diagnostics",
            style="Secondary.TButton",
            command=self._open_diagnostics,
        )
        self.diagnostics_button.pack(side="right", padx=(10, 0), pady=(8, 0))

        self.connection_label = tk.Label(
            header,
            text="●  Checking Chrome",
            bg=BG,
            fg=WARN,
            font=("Segoe UI Semibold", 10),
        )
        self.connection_label.pack(side="right", pady=(13, 0))

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=26, pady=(0, 24))
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, minsize=296)
        body.grid_rowconfigure(0, weight=1)

        self.main = tk.Frame(body, bg=BG)
        self.main.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        self.main.grid_rowconfigure(0, weight=1)
        self.main.grid_columnconfigure(0, weight=1)

        self.sidebar = tk.Frame(
            body,
            bg=PANEL,
            width=296,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        self.sidebar.grid(row=0, column=1, sticky="nsew")
        self.sidebar.grid_propagate(False)

        self._build_idle_panel()
        self._build_startup_panel()
        self._build_conversation_panel()
        self._build_sidebar()

    def _panel(self, parent):
        return tk.Frame(
            parent,
            bg=PANEL,
            highlightthickness=1,
            highlightbackground=BORDER,
        )

    def _build_idle_panel(self):
        self.idle_panel = self._panel(self.main)
        content = tk.Frame(self.idle_panel, bg=PANEL)
        content.pack(fill="both", expand=True, padx=28, pady=26)

        tk.Label(
            content,
            text="New Conversation",
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 19),
        ).pack(anchor="w")
        tk.Label(
            content,
            text=(
                "Give A and B a question, problem, or topic. Choose a fresh "
                "conversation or an eligible open ChatGPT tab for each role."
            ),
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 10),
            justify="left",
            wraplength=760,
        ).pack(anchor="w", pady=(6, 18))

        self._build_participant_setup(content)

        tk.Label(
            content,
            text="Conversation prompt",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w")

        self.prompt_text = tk.Text(
            content,
            height=12,
            wrap="word",
            bg=INPUT,
            fg=TEXT,
            insertbackground=TEXT,
            selectbackground="#30466d",
            relief="flat",
            padx=14,
            pady=12,
            font=("Segoe UI", 11),
            undo=True,
        )
        self.prompt_text.pack(fill="x", pady=(7, 18))

        bottom = tk.Frame(content, bg=PANEL)
        bottom.pack(fill="x")

        rounds_wrap = tk.Frame(bottom, bg=PANEL)
        rounds_wrap.pack(side="left")
        tk.Label(
            rounds_wrap,
            text="Initial rounds",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 9),
        ).pack(anchor="w")
        self.rounds_var = tk.StringVar(value="3")
        self.rounds_entry = tk.Spinbox(
            rounds_wrap,
            from_=1,
            to=999,
            width=7,
            textvariable=self.rounds_var,
            bg=INPUT,
            fg=TEXT,
            insertbackground=TEXT,
            buttonbackground=PANEL_ALT,
            relief="flat",
            justify="center",
            font=("Segoe UI", 11),
        )
        self.rounds_entry.pack(anchor="w", pady=(6, 0), ipady=5)

        self.start_button = ttk.Button(
            bottom,
            text="Start Parley",
            style="Primary.TButton",
            command=self.start_parley,
        )
        self.start_button.pack(side="right", anchor="s")

        note = tk.Frame(
            content,
            bg=PANEL_ALT,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        note.pack(fill="x", pady=(28, 0))
        tk.Label(
            note,
            text="WHAT HAPPENS NEXT",
            bg=PANEL_ALT,
            fg=MUTED,
            font=("Segoe UI Semibold", 8),
        ).pack(anchor="w", padx=14, pady=(12, 5))
        tk.Label(
            note,
            text=(
                "Parley resolves A and B from the choices above, creates any fresh "
                "participants, provisions and verifies the A/B protocols, then starts "
                "the relay. Existing and fresh chats can be mixed independently."
            ),
            bg=PANEL_ALT,
            fg=TEXT,
            font=("Segoe UI", 9),
            justify="left",
            wraplength=760,
        ).pack(anchor="w", padx=14, pady=(0, 13))

    def _build_participant_setup(self, parent):
        frame = tk.Frame(
            parent,
            bg=PANEL_ALT,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        frame.pack(fill="x", pady=(0, 18))

        head = tk.Frame(frame, bg=PANEL_ALT)
        head.pack(fill="x", padx=14, pady=(12, 8))
        tk.Label(
            head,
            text="Participants",
            bg=PANEL_ALT,
            fg=TEXT,
            font=("Segoe UI Semibold", 10),
        ).pack(side="left")
        self.refresh_tabs_button = ttk.Button(
            head,
            text="Refresh open tabs",
            style="Secondary.TButton",
            command=self._refresh_participant_tabs_async,
        )
        self.refresh_tabs_button.pack(side="right")

        for label, color in (("A", A_COLOR), ("B", B_COLOR)):
            row = tk.Frame(frame, bg=PANEL_ALT)
            row.pack(fill="x", padx=14, pady=(0, 10))
            row.grid_columnconfigure(2, weight=1)

            tk.Label(
                row,
                text=label,
                bg=PANEL_ALT,
                fg=color,
                font=("Segoe UI Semibold", 11),
                width=2,
            ).grid(row=0, column=0, sticky="w", padx=(0, 8))

            source_var = tk.StringVar(value="Fresh chat")
            source_box = ttk.Combobox(
                row,
                textvariable=source_var,
                values=("Fresh chat", "Existing chat"),
                state="readonly",
                width=14,
            )
            source_box.grid(row=0, column=1, sticky="w", padx=(0, 8))
            source_box.bind(
                "<<ComboboxSelected>>",
                lambda _event, role=label: self._participant_source_changed(role),
            )

            tab_var = tk.StringVar(value="")
            tab_box = ttk.Combobox(
                row,
                textvariable=tab_var,
                values=(),
                state="disabled",
            )
            tab_box.grid(row=0, column=2, sticky="ew")

            self._participant_source_vars[label] = source_var
            self._participant_tab_vars[label] = tab_var
            self._participant_tab_boxes[label] = tab_box

        tk.Label(
            frame,
            text=(
                "Each role can use a fresh conversation or any eligible "
                "ChatGPT tab already open in this Chrome session."
            ),
            bg=PANEL_ALT,
            fg=MUTED,
            font=("Segoe UI", 8),
            justify="left",
        ).pack(anchor="w", padx=14, pady=(0, 12))

    def _participant_source_changed(self, label):
        source = self._participant_source_vars[label].get()
        box = self._participant_tab_boxes[label]
        if source == "Existing chat":
            box.configure(state="readonly")
            if not self._participant_tabs:
                self._refresh_participant_tabs_async()
        else:
            box.configure(state="disabled")

    def _tab_display_name(self, tab):
        title = (tab.get("title") or "Untitled ChatGPT").strip()
        url = tab.get("url") or ""
        target = tab.get("id") or ""
        return f"{title}  ·  {url}  ·  {target}"

    def _refresh_participant_tabs_async(self):
        if hasattr(self, "refresh_tabs_button"):
            self.refresh_tabs_button.configure(state="disabled")

        def worker():
            try:
                tabs = core.list_tabs()
                if isinstance(tabs, dict):
                    raise RuntimeError(
                        tabs.get("error") or str(tabs)
                    )
                eligible = eligible_chatgpt_tabs(tabs)
                self._post(
                    self._participant_tabs_refreshed,
                    eligible,
                    None,
                )
            except Exception as exc:
                self._post(
                    self._participant_tabs_refreshed,
                    [],
                    str(exc),
                )

        threading.Thread(
            target=worker,
            name="parley-participant-tabs",
            daemon=True,
        ).start()

    def _participant_tabs_refreshed(self, tabs, error):
        if hasattr(self, "refresh_tabs_button"):
            self.refresh_tabs_button.configure(state="normal")

        previous = {
            label: self._participant_tab_vars[label].get()
            for label in ("A", "B")
        }
        self._participant_tabs = list(tabs or [])
        self._participant_tab_map = {
            self._tab_display_name(tab): tab
            for tab in self._participant_tabs
        }
        values = tuple(self._participant_tab_map.keys())

        for label in ("A", "B"):
            box = self._participant_tab_boxes[label]
            box.configure(values=values)
            old = previous[label]
            if old in self._participant_tab_map:
                self._participant_tab_vars[label].set(old)
            else:
                self._participant_tab_vars[label].set("")

        if error:
            self._diagnostic(
                f"Could not refresh participant tabs: {error}"
            )
        else:
            self._diagnostic(
                f"Found {len(self._participant_tabs)} eligible ChatGPT tab(s)"
            )

    def _participant_specs(self):
        specs = {}
        for label in ("A", "B"):
            source = self._participant_source_vars[label].get()
            if source == "Fresh chat":
                specs[label] = {"source": "fresh"}
                continue

            display = self._participant_tab_vars[label].get()
            tab = self._participant_tab_map.get(display)
            if tab is None:
                return None, (
                    f"Choose an open ChatGPT tab for participant {label}."
                )
            specs[label] = {
                "source": "existing",
                "tab": dict(tab),
            }

        if (
            specs["A"]["source"] == "existing"
            and specs["B"]["source"] == "existing"
            and specs["A"]["tab"]["id"] == specs["B"]["tab"]["id"]
        ):
            return None, "Participants A and B must use different tabs."

        return specs, None

    def _set_participant_identity(self, label, tab):
        target = (
            self.participant_a_title
            if label == "A"
            else self.participant_b_title
        )
        title = (tab.get("title") or "").strip()
        source = tab.get("source")
        if not title:
            title = (
                f"Fresh ChatGPT {label}"
                if source == "fresh"
                else f"ChatGPT {label}"
            )
        target.configure(text=title)

    def _build_startup_panel(self):
        self.startup_panel = self._panel(self.main)
        content = tk.Frame(self.startup_panel, bg=PANEL)
        content.pack(fill="both", expand=True, padx=30, pady=28)

        tk.Label(
            content,
            text="Preparing Conversation",
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 19),
        ).pack(anchor="w")

        self.startup_stage = tk.Label(
            content,
            text="Creating participants…",
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI", 12),
            justify="left",
        )
        self.startup_stage.pack(anchor="w", pady=(12, 18))

        self.startup_progress = ttk.Progressbar(
            content,
            mode="indeterminate",
            style="Parley.Horizontal.TProgressbar",
        )
        self.startup_progress.pack(fill="x")

        checklist = tk.Frame(content, bg=PANEL)
        checklist.pack(fill="x", pady=(28, 0))
        self.startup_a = self._startup_row(checklist, "A", A_COLOR)
        self.startup_b = self._startup_row(checklist, "B", B_COLOR)

        self.cancel_start_button = ttk.Button(
            content,
            text="Cancel",
            style="Secondary.TButton",
            command=self.stop_session,
        )
        self.cancel_start_button.pack(anchor="e", pady=(26, 0))

    def _startup_row(self, parent, label, color):
        row = tk.Frame(
            parent,
            bg=PANEL_ALT,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        row.pack(fill="x", pady=(0, 10))
        tk.Label(
            row,
            text=label,
            bg=PANEL_ALT,
            fg=color,
            font=("Segoe UI Semibold", 12),
            width=3,
        ).pack(side="left", padx=(12, 4), pady=12)
        value = tk.Label(
            row,
            text="Waiting",
            bg=PANEL_ALT,
            fg=MUTED,
            font=("Segoe UI", 10),
        )
        value.pack(side="left", padx=8)
        return value

    def _build_conversation_panel(self):
        self.conversation_panel = self._panel(self.main)
        head = tk.Frame(self.conversation_panel, bg=PANEL)
        head.pack(fill="x", padx=18, pady=(14, 10))

        tk.Label(
            head,
            text="Conversation",
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 14),
        ).pack(side="left")

        self.live_badge = tk.Label(
            head,
            text="● LIVE",
            bg=PANEL,
            fg=SUCCESS,
            font=("Segoe UI Semibold", 9),
        )
        self.live_badge.pack(side="right")

        wrap = tk.Frame(self.conversation_panel, bg=PANEL)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.transcript = tk.Text(
            wrap,
            wrap="word",
            bg=INPUT,
            fg=TEXT,
            relief="flat",
            padx=18,
            pady=16,
            font=("Segoe UI", 10),
            state="disabled",
            cursor="arrow",
        )
        scroll = ttk.Scrollbar(
            wrap,
            orient="vertical",
            command=self.transcript.yview,
        )
        self.transcript.configure(yscrollcommand=scroll.set)
        self.transcript.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.transcript.tag_configure(
            "a_head",
            foreground=A_COLOR,
            font=("Segoe UI Semibold", 10),
            spacing1=14,
            spacing3=4,
        )
        self.transcript.tag_configure(
            "b_head",
            foreground=B_COLOR,
            font=("Segoe UI Semibold", 10),
            spacing1=14,
            spacing3=4,
        )
        self.transcript.tag_configure(
            "human_head",
            foreground=SUCCESS,
            font=("Segoe UI Semibold", 9),
            spacing1=10,
            spacing3=4,
        )
        self.transcript.tag_configure(
            "body",
            foreground=TEXT,
            font=("Segoe UI", 10),
            lmargin1=8,
            lmargin2=8,
            rmargin=8,
            spacing3=10,
        )
        self.transcript.tag_configure(
            "system",
            foreground=MUTED,
            font=("Segoe UI", 9, "italic"),
            spacing1=8,
            spacing3=8,
        )

    def _build_sidebar(self):
        pad = tk.Frame(self.sidebar, bg=PANEL)
        pad.pack(fill="both", expand=True, padx=16, pady=18)

        tk.Label(
            pad,
            text="Participants",
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 12),
        ).pack(anchor="w")

        (
            self.participant_a,
            self.participant_a_title,
        ) = self._participant_card(
            pad, "A", A_COLOR, "ChatGPT A"
        )
        (
            self.participant_b,
            self.participant_b_title,
        ) = self._participant_card(
            pad, "B", B_COLOR, "ChatGPT B"
        )

        tk.Label(
            pad,
            text="Session",
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 12),
        ).pack(anchor="w", pady=(18, 8))

        status_card = tk.Frame(
            pad,
            bg=PANEL_ALT,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        status_card.pack(fill="x")

        self.state_value = self._status_row(status_card, "State", "Ready")
        self.phase_value = self._status_row(status_card, "Phase", "New conversation")
        self.round_value = self._status_row(status_card, "Round", "0 / 0")
        self.transfer_value = self._status_row(status_card, "Transfers", "0")
        self.elapsed_value = self._status_row(status_card, "Elapsed", "00:00", last=True)

        self.control_frame = tk.Frame(pad, bg=PANEL)
        self.control_frame.pack(fill="x", pady=(14, 0))

        row = tk.Frame(self.control_frame, bg=PANEL)
        row.pack(fill="x")
        self.pause_button = ttk.Button(
            row,
            text="Pause",
            style="Secondary.TButton",
            command=self.pause_session,
        )
        self.pause_button.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.resume_button = ttk.Button(
            row,
            text="Resume",
            style="Secondary.TButton",
            command=self.resume_session,
        )
        self.resume_button.pack(side="left", fill="x", expand=True, padx=(4, 0))

        self.continue_button = ttk.Button(
            self.control_frame,
            text="Continue +1 Round",
            style="Primary.TButton",
            command=self.continue_one_round,
        )
        self.continue_button.pack(fill="x", pady=(8, 0))

        self.add_rounds_button = ttk.Button(
            self.control_frame,
            text="Add Rounds…",
            style="Secondary.TButton",
            command=self.add_rounds,
        )
        self.add_rounds_button.pack(fill="x", pady=(7, 0))

        self.finish_button = ttk.Button(
            self.control_frame,
            text="Finish Here",
            style="Secondary.TButton",
            command=self.finish_here,
        )
        self.finish_button.pack(fill="x", pady=(7, 0))

        self.stop_button = ttk.Button(
            self.control_frame,
            text="Stop Session",
            style="Danger.TButton",
            command=self.stop_session,
        )
        self.stop_button.pack(fill="x", pady=(12, 0))

        self.new_button = ttk.Button(
            self.control_frame,
            text="New Conversation",
            style="Primary.TButton",
            command=self.new_conversation,
        )
        self.new_button.pack(fill="x", pady=(8, 0))

    def _participant_card(self, parent, label, color, title):
        card = tk.Frame(
            parent,
            bg=PANEL_ALT,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        card.pack(fill="x", pady=(8, 0))

        top = tk.Frame(card, bg=PANEL_ALT)
        top.pack(fill="x", padx=11, pady=(10, 3))
        tk.Label(
            top,
            text=label,
            bg=PANEL_ALT,
            fg=color,
            font=("Segoe UI Semibold", 11),
        ).pack(side="left")
        state = tk.Label(
            top,
            text="Waiting",
            bg=PANEL_ALT,
            fg=MUTED,
            font=("Segoe UI Semibold", 8),
        )
        state.pack(side="right")
        title_label = tk.Label(
            card,
            text=title,
            bg=PANEL_ALT,
            fg=MUTED,
            font=("Segoe UI", 8),
            justify="left",
            wraplength=250,
        )
        title_label.pack(anchor="w", padx=11, pady=(0, 10))
        return state, title_label

    def _status_row(self, parent, label, value, last=False):
        row = tk.Frame(parent, bg=PANEL_ALT)
        row.pack(
            fill="x",
            padx=11,
            pady=(8 if label == "State" else 3, 9 if last else 3),
        )
        tk.Label(
            row,
            text=label,
            bg=PANEL_ALT,
            fg=MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left")
        target = tk.Label(
            row,
            text=value,
            bg=PANEL_ALT,
            fg=TEXT,
            font=("Segoe UI Semibold", 9),
        )
        target.pack(side="right")
        return target

    def _set_mode(self, mode):
        for panel in (
            self.idle_panel,
            self.startup_panel,
            self.conversation_panel,
        ):
            panel.grid_forget()

        if mode == "idle":
            self.idle_panel.grid(row=0, column=0, sticky="nsew")
        elif mode == "startup":
            self.startup_panel.grid(row=0, column=0, sticky="nsew")
        else:
            self.conversation_panel.grid(row=0, column=0, sticky="nsew")
        self._mode = mode
        self._update_controls()

    def _set_participant(self, label, text, color):
        target = self.participant_a if label == "A" else self.participant_b
        target.configure(text=text, fg=color)
        startup = self.startup_a if label == "A" else self.startup_b
        startup.configure(text=text, fg=color)

    def _update_session_card(
        self,
        *,
        state=None,
        phase=None,
        rounds=None,
        transfers=None,
    ):
        if state is not None:
            self.state_value.configure(text=state)
        if phase is not None:
            self.phase_value.configure(text=phase)
        if rounds is not None:
            self.round_value.configure(text=rounds)
        if transfers is not None:
            self.transfer_value.configure(text=transfers)

    def _post(self, fn, *args):
        self._ui_queue.put((fn, args))

    def _drain_ui_queue(self):
        try:
            while True:
                fn, args = self._ui_queue.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        if not self._closing:
            self.root.after(self.UI_DRAIN_MS, self._drain_ui_queue)

    def _diagnostic(self, text):
        line = f"{time.strftime('%H:%M:%S')}  {text}"
        self._diagnostic_lines.append(line)
        if self._diagnostic_text is not None:
            try:
                self._diagnostic_text.configure(state="normal")
                self._diagnostic_text.insert("end", line + "\n")
                self._diagnostic_text.configure(state="disabled")
                self._diagnostic_text.see("end")
            except tk.TclError:
                self._diagnostic_text = None
                self._diagnostic_window = None

    def _open_diagnostics(self):
        if self._diagnostic_window is not None:
            try:
                self._diagnostic_window.lift()
                self._diagnostic_window.focus_force()
                return
            except tk.TclError:
                self._diagnostic_window = None

        win = tk.Toplevel(self.root)
        win.title("Parley Diagnostics")
        win.geometry("860x520")
        win.configure(bg=BG)
        self._diagnostic_window = win

        text = tk.Text(
            win,
            wrap="word",
            bg=INPUT,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            padx=12,
            pady=12,
            font=("Cascadia Mono", 9),
        )
        text.pack(fill="both", expand=True, padx=14, pady=14)
        text.insert("1.0", "\n".join(self._diagnostic_lines))
        if self._diagnostic_lines:
            text.insert("end", "\n")
        text.configure(state="disabled")
        self._diagnostic_text = text

        def closed():
            self._diagnostic_text = None
            self._diagnostic_window = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", closed)

    def _check_browser_async(self):
        def worker():
            try:
                value = core.list_tabs()
                ok = isinstance(value, list)
                detail = None if ok else str(value)
            except Exception as exc:
                ok = False
                detail = str(exc)
            self._post(self._browser_checked, ok, detail)

        threading.Thread(
            target=worker,
            name="parley-browser-check",
            daemon=True,
        ).start()

    def _browser_checked(self, ok, detail):
        if ok:
            self.connection_label.configure(
                text="●  Chrome connected",
                fg=SUCCESS,
            )
            self._diagnostic("Chrome connection ready")
            self._refresh_participant_tabs_async()
        else:
            self.connection_label.configure(
                text="●  Chrome unavailable",
                fg=DANGER,
            )
            self._diagnostic(f"Chrome connection check failed: {detail}")

    def _rounds(self):
        try:
            value = int(self.rounds_var.get())
        except ValueError:
            return None
        return value if value > 0 else None

    def start_parley(self):
        if self._mode != "idle":
            return

        prompt = self.prompt_text.get("1.0", "end-1c").strip()
        rounds = self._rounds()
        if not prompt:
            messagebox.showwarning(
                "Parley",
                "Enter a conversation prompt first.",
            )
            return
        if rounds is None:
            messagebox.showwarning(
                "Parley",
                "Initial rounds must be a positive whole number.",
            )
            return

        participant_specs, participant_error = self._participant_specs()
        if participant_error:
            messagebox.showwarning(
                "Parley",
                participant_error,
            )
            return

        self._clear_transcript()
        self.startup_stage.configure(text="Creating participants…")
        self._set_participant("A", "Waiting", MUTED)
        self._set_participant("B", "Waiting", MUTED)
        self._update_session_card(
            state="Starting",
            phase="Creating participants",
            rounds=f"0 / {rounds}",
            transfers="0",
        )
        self._set_mode("startup")
        self.startup_progress.start(12)
        summary = ", ".join(
            f"{label}={participant_specs[label]['source']}"
            for label in ("A", "B")
        )
        self._diagnostic(f"Starting Parley session ({summary})")
        for label in ("A", "B"):
            spec = participant_specs[label]
            if spec["source"] == "fresh":
                self._set_participant_identity(
                    label,
                    {
                        "title": f"Fresh ChatGPT {label}",
                        "source": "fresh",
                    },
                )
            else:
                self._set_participant_identity(label, spec["tab"])

        def progress(event):
            self._post(self._startup_progress_event, dict(event))

        def finished(result, completed_prompt, completed_rounds):
            self._post(
                self._startup_finished,
                result,
                completed_prompt,
                completed_rounds,
            )

        self.controller.start(
            prompt,
            participant_specs,
            rounds,
            progress=progress,
            finished=finished,
        )
        self._update_controls()

    def _startup_progress_event(self, event):
        stage = event.get("stage")
        status = event.get("status")
        label = event.get("label")

        if stage == "participants":
            text = "Resolving conversation participants…"
        elif stage == "participant" and label:
            source = event.get("source")
            text = (
                f"Creating fresh Chat {label}…"
                if source == "fresh"
                else f"Using selected Chat {label}…"
            )
            if status == "complete":
                self._set_participant(
                    label,
                    "Created" if source == "fresh" else "Selected",
                    SUCCESS,
                )
        elif stage == "fresh_chats":
            text = "Creating fresh ChatGPT conversations…"
        elif stage == "focus_emulation" and label:
            text = f"Keeping Chat {label} active in the background…"
            self._set_participant(label, "Background active", SUCCESS)
        elif stage == "initial_prompt" and label:
            text = f"Establishing Chat {label}…"
            if status == "complete":
                self._set_participant(label, "Established", SUCCESS)
        elif stage == "protocols":
            text = "Preparing Parley protocols…"
        elif stage == "preflight" and label:
            text = f"Checking Chat {label}…"
        elif stage == "provision" and label:
            text = f"Uploading protocol to Chat {label}…"
            self._set_participant(label, "Uploading", WARN)
        elif stage == "attachment_stabilizing" and label:
            seconds = event.get("seconds", 0)
            text = (
                f"Finishing Chat {label} protocol upload "
                f"({seconds:g}s minimum)…"
            )
            self._set_participant(label, "Finishing upload", WARN)
        elif stage == "protocol_ack" and label:
            text = f"Verifying Chat {label} protocol…"
            self._set_participant(label, "Verifying", WARN)
        elif stage == "protocol_ready" and label:
            text = f"Chat {label} protocol verified."
            self._set_participant(label, "Verified", SUCCESS)
        elif stage == "activation" and label:
            text = f"Activating Chat {label}…"
            self._set_participant(label, "Activating", WARN)
        elif stage == "ready" and label:
            text = f"Chat {label} ready."
            self._set_participant(label, "Ready", SUCCESS)
        elif stage == "session_prompt":
            text = "Starting the conversation with Chat A…"
        elif stage == "reset_propagation":
            text = (
                "Synchronizing RESET CHAT to Chat B…"
                if status == "starting"
                else "RESET CHAT synchronized."
            )
            if label:
                self._set_participant(
                    label,
                    "Resetting" if status == "starting" else "Reset",
                    WARN if status == "starting" else SUCCESS,
                )
        else:
            text = "Preparing conversation…"

        self.startup_stage.configure(text=text)
        self.phase_value.configure(text=text.rstrip("…"))
        self._diagnostic(text.rstrip("…"))
        activity = event.get("page_activity")
        if label and isinstance(activity, dict):
            self._diagnostic(
                f"Chat {label} renderer: "
                f"visibility={activity.get('visibilityState')} "
                f"hidden={activity.get('hidden')} "
                f"hasFocus={activity.get('hasFocus')}"
            )

    def _startup_finished(self, result, prompt, rounds):
        self.startup_progress.stop()
        if not isinstance(result, dict) or not result.get("ok"):
            error = (
                result.get("error")
                if isinstance(result, dict)
                else str(result)
            )
            stage = (
                result.get("stage", "startup")
                if isinstance(result, dict)
                else "startup"
            )
            if (
                error == "chatgpt_wait_stopped"
                or self.controller.operation_stop.is_set()
            ):
                self._diagnostic("Startup cancelled by user")
                self._update_session_card(
                    state="Stopped",
                    phase="Startup cancelled",
                )
                self._set_mode("idle")
                self._set_participant("A", "Waiting", MUTED)
                self._set_participant("B", "Waiting", MUTED)
                return

            self._diagnostic(f"Startup failed at {stage}: {error}")
            self._update_session_card(
                state="Error",
                phase="Startup failed",
            )
            self._set_mode("idle")
            messagebox.showerror(
                "Could not start Parley",
                f"Startup failed during {stage}.\n\n"
                f"{error or 'Unknown error'}",
            )
            return

        if (
            isinstance(result, dict)
            and result.get("ok")
            and result.get("reset_requested")
            and result.get("reset_propagated")
        ):
            tab_a = result["A"]
            tab_b = result["B"]
            self._set_participant_identity("A", tab_a)
            self._set_participant_identity("B", tab_b)
            self._set_participant("A", "Reset", SUCCESS)
            self._set_participant("B", "Reset", SUCCESS)
            self.live_badge.configure(text="● RESET", fg=MUTED)
            restart = result.get("restart_point", "A")
            self._update_session_card(
                state="Reset",
                phase=f"Restart at Chat {restart}",
            )
            self._diagnostic(
                "Reset coordinated: "
                f"{result.get('reset_by', 'A')} → "
                f"{result.get('reset_acknowledged_by', 'B')}; "
                f"restart point {restart}"
            )
            self._set_mode("idle")
            self._update_controls()
            return

        tab_a = result["A"]
        tab_b = result["B"]
        initial_text = result.get("initial_response_text") or ""
        self._diagnostic(f"Chat A target: {tab_a.get('id')}")
        self._diagnostic(f"Chat B target: {tab_b.get('id')}")
        self._set_participant_identity("A", tab_a)
        self._set_participant_identity("B", tab_b)

        self._append_transcript("H", "Session brief", prompt)
        self._append_transcript(
            "A",
            "A",
            _display_reply(initial_text, "A"),
        )

        self.controller.create_relay_session(
            result,
            prompt,
            rounds,
        )
        self._set_participant("A", "Ready", SUCCESS)
        self._set_participant("B", "Ready", SUCCESS)
        self._set_mode("conversation")
        self.live_badge.configure(text="● LIVE", fg=SUCCESS)
        self._update_session_card(
            state="Live",
            phase="Relay starting",
            rounds=f"0 / {rounds}",
            transfers="0",
        )
        self._diagnostic(
            "Protocol bootstrap complete; relay starting"
        )
        self.controller.start_relay_session()
        self._update_controls()

    def pause_session(self):
        if self.controller.pause():
            self._diagnostic("Pause requested")
            self._update_controls()

    def resume_session(self):
        if self.controller.resume():
            self._diagnostic("Resume requested")
            self._update_controls()

    def continue_one_round(self):
        if not self.controller.session_alive:
            return
        status = self.controller.status()
        current = int(status.get("rounds_requested", 0) or 0)
        try:
            updated = self.controller.extend_rounds(current + 1)
        except Exception as exc:
            messagebox.showwarning("Parley", str(exc))
            return
        self._diagnostic(f"Round limit extended to {updated}")
        self._update_controls()

    def add_rounds(self):
        if not self.controller.session_alive:
            return
        status = self.controller.status()
        current = int(status.get("rounds_requested", 0) or 0)
        additional = simpledialog.askinteger(
            "Add Rounds",
            "How many additional rounds?",
            parent=self.root,
            minvalue=1,
            maxvalue=999,
        )
        if additional is None:
            return
        try:
            updated = self.controller.extend_rounds(
                current + additional
            )
        except Exception as exc:
            messagebox.showwarning("Parley", str(exc))
            return
        self._diagnostic(f"Round limit extended to {updated}")
        self._update_controls()

    def finish_here(self):
        if self.controller.finish_at_round_limit():
            self._diagnostic(
                "Finish requested at current round boundary"
            )
            self._update_controls()

    def stop_session(self):
        stopped = self.controller.stop()
        if stopped == "startup":
            self.startup_stage.configure(
                text="Stopping after the current safe operation…"
            )
            self._update_session_card(
                state="Stopping",
                phase="Stopping safely",
            )
            self._diagnostic("Startup stop requested")
            self._update_controls()
            return

        if stopped == "session":
            self.live_badge.configure(
                text="● STOPPING",
                fg=WARN,
            )
            self._update_session_card(
                state="Stopping",
                phase="Finishing current operation",
            )
            self._diagnostic(
                "Stop requested; no later transfer will begin"
            )
            self._update_controls()

    def new_conversation(self):
        if not self.controller.reset():
            return

        self._clear_transcript()
        self._set_participant("A", "Waiting", MUTED)
        self._set_participant("B", "Waiting", MUTED)
        self._update_session_card(
            state="Ready",
            phase="New conversation",
            rounds="0 / 0",
            transfers="0",
        )
        self._set_mode("idle")
        self._refresh_participant_tabs_async()
        self.prompt_text.focus_set()

    def _poll(self):
        try:
            self._poll_session()
            self._tick_elapsed()
            self._update_controls()
        finally:
            if not self._closing:
                self.root.after(self.POLL_MS, self._poll)

    def _poll_session(self):
        snapshot = self.controller.poll()
        if not snapshot:
            return

        status = snapshot["status"]
        rounds_done = int(
            status.get("rounds_completed", 0) or 0
        )
        rounds_total = int(
            status.get("rounds_requested", 0) or 0
        )
        transfers = int(
            status.get("transfers_completed", 0) or 0
        )
        phase = _phase_from_status(status)
        state = (
            "Paused"
            if status.get("control") == "paused"
            else "Live"
        )
        if status.get("awaiting_extension"):
            state = "Round complete"
        if not self.controller.session_alive:
            state = (
                status.get("status") or "Ended"
            ).title()

        self._update_session_card(
            state=state,
            phase=phase,
            rounds=f"{rounds_done} / {rounds_total}",
            transfers=str(transfers),
        )

        for item in snapshot["transfers"]:
            direction = item.get("direction")
            label = "B" if direction == "A->B" else "A"
            text = _display_reply(
                item.get("response_text"),
                label,
            )
            self._append_transcript(label, label, text)
            self._diagnostic(
                "Completed round {round} {direction}: "
                "{source_chars} → {response_chars} chars".format(
                    **item
                )
            )
            activity = item.get("response_page_activity")
            if isinstance(activity, dict):
                self._diagnostic(
                    f"Chat {label} renderer: "
                    f"visibility={activity.get('visibilityState')} "
                    f"hidden={activity.get('hidden')} "
                    f"hasFocus={activity.get('hasFocus')}"
                )

        for event in snapshot["events"]:
            if event.get("event") in {
                "relay_round_limit_reached",
                "relay_round_limit_extended",
                "relay_reset_requested",
                "relay_reset_propagated",
                "relay_error",
                "relay_stopped",
            }:
                self._diagnostic(
                    self._event_summary(event)
                )

        if snapshot["finished"]:
            self._session_finished(
                snapshot["result"],
                snapshot["exception"],
            )

    def _session_finished(self, result, exception):

        if exception is not None:
            self.live_badge.configure(
                text="● ERROR",
                fg=DANGER,
            )
            self._update_session_card(
                state="Error",
                phase="Relay failed",
            )
            self._append_system(
                f"Session interrupted: {exception}"
            )
            self._diagnostic(
                f"Relay worker failed: {exception}"
            )
            messagebox.showerror(
                "Parley relay failed",
                str(exception),
            )
            return

        result = result if isinstance(result, dict) else {}
        status = result.get("status", "finished")
        if status == "error":
            error = result.get(
                "error",
                "Unknown relay error",
            )
            self.live_badge.configure(
                text="● ERROR",
                fg=DANGER,
            )
            self._update_session_card(
                state="Error",
                phase="Relay interrupted",
            )
            self._append_system(
                f"Session interrupted: {error}"
            )
            self._diagnostic(
                f"Relay error: {error} "
                f"({result.get('stage', 'unknown')})"
            )
            return

        if (
            status == "stopped"
            and self.controller.user_stop_requested
        ):
            headline = "Session stopped by user."
        elif status == "complete":
            headline = "Session complete."
        else:
            headline = f"Session {status}."

        self.live_badge.configure(
            text="● ENDED",
            fg=MUTED,
        )
        self._update_session_card(
            state="Ended",
            phase=headline.rstrip("."),
        )
        self._append_system(
            f"{headline} "
            f"{result.get('rounds_completed', 0)} "
            "complete round(s)."
        )
        self._diagnostic(headline)
        self._update_controls()

    def _event_summary(self, event):
        kind = event.get("event")
        if kind == "relay_round_limit_reached":
            return (
                "Round limit reached at "
                f"{event.get('round_limit')}"
            )
        if kind == "relay_round_limit_extended":
            return (
                "Round limit extended to "
                f"{event.get('round_limit')}"
            )
        if kind == "relay_error":
            return (
                f"Relay error: {event.get('error')} "
                f"({event.get('stage')})"
            )
        if kind == "relay_stopped":
            return (
                "Relay stopped at "
                f"{event.get('stage')}"
            )
        if kind == "relay_reset_requested":
            return (
                "Reset requested by Chat "
                f"{event.get('chat')}"
            )
        if kind == "relay_reset_propagated":
            return (
                "Reset propagated "
                f"{event.get('source_chat')} → "
                f"{event.get('destination_chat')}"
            )
        return str(kind or "event")

    def _append_transcript(self, label, name, text):
        if not str(text or "").strip():
            return
        self.transcript.configure(state="normal")
        tag = {
            "A": "a_head",
            "B": "b_head",
            "H": "human_head",
        }.get(label, "human_head")
        self.transcript.insert(
            "end",
            f"{name}\n",
            tag,
        )
        self.transcript.insert(
            "end",
            str(text).strip() + "\n",
            "body",
        )
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _append_system(self, text):
        self.transcript.configure(state="normal")
        self.transcript.insert(
            "end",
            str(text).strip() + "\n",
            "system",
        )
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _clear_transcript(self):
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")

    def _tick_elapsed(self):
        seconds = self.controller.elapsed_seconds()
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            value = (
                f"{hours:02d}:"
                f"{minutes:02d}:"
                f"{seconds:02d}"
            )
        else:
            value = f"{minutes:02d}:{seconds:02d}"
        self.elapsed_value.configure(text=value)

    def _update_controls(self):
        startup_alive = self.controller.startup_alive
        session_alive = self.controller.session_alive
        status = (
            self.controller.status()
            if session_alive
            else {}
        )
        awaiting = bool(
            status.get("awaiting_extension")
        )
        paused = (
            status.get("control") == "paused"
        )

        self.start_button.configure(
            state=(
                "normal"
                if self._mode == "idle"
                else "disabled"
            )
        )
        self.cancel_start_button.configure(
            state=(
                "normal"
                if startup_alive
                else "disabled"
            )
        )
        self.pause_button.configure(
            state=(
                "normal"
                if session_alive and not paused
                else "disabled"
            )
        )
        self.resume_button.configure(
            state=(
                "normal"
                if session_alive and paused
                else "disabled"
            )
        )
        boundary_state = (
            "normal"
            if session_alive and awaiting
            else "disabled"
        )
        self.continue_button.configure(
            state=boundary_state
        )
        self.add_rounds_button.configure(
            state=boundary_state
        )
        self.finish_button.configure(
            state=boundary_state
        )
        self.stop_button.configure(
            state=(
                "normal"
                if startup_alive or session_alive
                else "disabled"
            )
        )
        ended = bool(
            self._mode == "conversation"
            and self.controller.has_session
            and not session_alive
            and self.controller.session_done_seen
        )
        self.new_button.configure(
            state=(
                "normal"
                if ended
                else "disabled"
            )
        )

    def _on_close(self):
        startup_alive = self.controller.startup_alive
        session_alive = self.controller.session_alive
        if startup_alive or session_alive:
            if not messagebox.askyesno(
                "Close Parley",
                "A Parley operation is active. "
                "Stop it and close?",
            ):
                return
            self._closing = True
            self.controller.stop()
            self._finish_close_when_safe()
            return

        self._closing = True
        self.root.destroy()

    def _finish_close_when_safe(self):
        startup_alive = self.controller.startup_alive
        session_alive = self.controller.session_alive
        if startup_alive or session_alive:
            self.root.after(
                100,
                self._finish_close_when_safe,
            )
            return
        self.root.destroy()


def launch():
    os.environ.setdefault(
        "PARLEY_CONNECTION_MODE",
        "live",
    )
    root = tk.Tk()
    ParleyApp(root)
    root.mainloop()
    return 0
