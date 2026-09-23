"""Parley desktop control console.

Tkinter is intentionally used here so the production UI remains dependency-free
and continues to drive the same relay engine as the CLI.
"""

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from . import core, workflows
from .relay import RelaySession
from .relay.engine import (
    RESET_CHAT_COMMAND,
    TEST_A_REPLY_PREFIX,
    TEST_A_REPLY_SUFFIX,
    TEST_B_REPLY_PREFIX,
    TEST_B_REPLY_SUFFIX,
)

BG = "#0b1020"
PANEL = "#111827"
PANEL_ALT = "#151f32"
BORDER = "#263449"
TEXT = "#e8eef7"
MUTED = "#91a0b5"
ACCENT = "#7c9cff"
ACCENT_HOVER = "#94adff"
A_COLOR = "#77bdfb"
B_COLOR = "#d7a7ff"
SUCCESS = "#55d6a5"
WARN = "#f0c36a"
DANGER = "#ff7a90"
INPUT = "#0e1627"


def _tab_label(tab):
    title = (tab.get("title") or "(untitled)").strip()
    tab_id = str(tab.get("id") or "")
    suffix = tab_id[-8:] if tab_id else "unknown"
    return f"{title}  ·  {suffix}"


def _display_reply(text, label):
    value = str(text or "").strip()
    if value == RESET_CHAT_COMMAND:
        return value

    if label == "A":
        prefix, suffix = TEST_A_REPLY_PREFIX, TEST_A_REPLY_SUFFIX
    else:
        prefix, suffix = TEST_B_REPLY_PREFIX, TEST_B_REPLY_SUFFIX

    if value.startswith(prefix):
        value = value[len(prefix):].lstrip()
    if value.endswith(suffix):
        value = value[:-len(suffix)].rstrip()
    return value


def _result_ok(result):
    return bool(
        isinstance(result, dict)
        and not result.get("error")
        and result.get("response_complete")
    )


class ParleyApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Parley")
        self.root.geometry("1320x840")
        self.root.minsize(1120, 720)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.tabs = []
        self.tab_by_label = {}
        self.ready = {"A": False, "B": False}
        self.session = None
        self._session_done_seen = False
        self._seen_transfers = 0
        self._seen_events = 0
        self._phase = "Idle"
        self._phase_started = time.monotonic()
        self._busy = False
        self._operation_stop = threading.Event()
        self._ui_queue = queue.Queue()
        self._init_pending = set()
        self._init_errors = {}

        self._build_style()
        self._build_ui()
        self._set_protocol_status("A", "Not initialized", MUTED)
        self._set_protocol_status("B", "Not initialized", MUTED)
        self._update_controls()

        self.root.after(80, self._drain_ui_queue)
        self.root.after(250, self._poll)
        self.refresh_tabs()

    def _on_close(self):
        active = bool(self.session and self.session.is_alive())
        if active:
            if not messagebox.askyesno(
                "Close Parley",
                "A relay is still active. Stop it and close Parley?",
            ):
                return
            self.session.stop()

        if self._busy:
            self._operation_stop.set()

        if active:
            self._activity("Closing Parley after relay stops")
            self.root.after(100, self._finish_close_when_safe)
        else:
            self.root.destroy()

    def _finish_close_when_safe(self):
        if self.session and self.session.is_alive():
            self.root.after(100, self._finish_close_when_safe)
            return
        self.root.destroy()

    def _build_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "Parley.TCombobox",
            fieldbackground=INPUT,
            background=PANEL_ALT,
            foreground=TEXT,
            arrowcolor=MUTED,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=7,
        )
        style.map(
            "Parley.TCombobox",
            fieldbackground=[("readonly", INPUT)],
            foreground=[("readonly", TEXT)],
            selectbackground=[("readonly", INPUT)],
            selectforeground=[("readonly", TEXT)],
        )
        style.configure(
            "Primary.TButton",
            font=("Segoe UI Semibold", 10),
            padding=(14, 9),
            background=ACCENT,
            foreground="#07101f",
            borderwidth=0,
        )
        style.map(
            "Primary.TButton",
            background=[
                ("active", ACCENT_HOVER),
                ("disabled", "#344057"),
            ],
            foreground=[("disabled", "#7f8b9d")],
        )
        style.configure(
            "Secondary.TButton",
            font=("Segoe UI", 10),
            padding=(12, 8),
            background=PANEL_ALT,
            foreground=TEXT,
            bordercolor=BORDER,
            borderwidth=1,
        )
        style.map(
            "Secondary.TButton",
            background=[("active", "#1c2940"), ("disabled", "#101827")],
            foreground=[("disabled", "#59677b")],
        )
        style.configure(
            "Danger.TButton",
            font=("Segoe UI Semibold", 10),
            padding=(12, 8),
            background="#4a1f2a",
            foreground="#ffd6dd",
            borderwidth=0,
        )
        style.map("Danger.TButton", background=[("active", "#632837")])

    def _build_ui(self):
        header = tk.Frame(self.root, bg=BG, height=76)
        header.pack(fill="x", padx=24, pady=(18, 8))
        header.pack_propagate(False)

        brand = tk.Frame(header, bg=BG)
        brand.pack(side="left", fill="y")
        tk.Label(
            brand,
            text="PARLEY",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 20),
        ).pack(anchor="w")
        tk.Label(
            brand,
            text="Two-chat conversation console",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(1, 0))

        self.connection_label = tk.Label(
            header,
            text="●  Connecting to Chrome",
            bg=BG,
            fg=WARN,
            font=("Segoe UI Semibold", 10),
        )
        self.connection_label.pack(side="right", pady=(9, 0))

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=24, pady=(0, 20))

        self.left = tk.Frame(body, bg=PANEL, width=310, highlightthickness=1, highlightbackground=BORDER)
        self.left.pack(side="left", fill="y")
        self.left.pack_propagate(False)

        center_wrap = tk.Frame(body, bg=BG)
        center_wrap.pack(side="left", fill="both", expand=True, padx=14)

        self.right = tk.Frame(body, bg=PANEL, width=285, highlightthickness=1, highlightbackground=BORDER)
        self.right.pack(side="right", fill="y")
        self.right.pack_propagate(False)

        self._build_left()
        self._build_center(center_wrap)
        self._build_right()

    def _section_title(self, parent, title, subtitle=None):
        frame = tk.Frame(parent, bg=parent.cget("bg"))
        tk.Label(
            frame,
            text=title,
            bg=parent.cget("bg"),
            fg=TEXT,
            font=("Segoe UI Semibold", 12),
        ).pack(anchor="w")
        if subtitle:
            tk.Label(
                frame,
                text=subtitle,
                bg=parent.cget("bg"),
                fg=MUTED,
                font=("Segoe UI", 9),
                wraplength=255,
                justify="left",
            ).pack(anchor="w", pady=(3, 0))
        return frame

    def _build_left(self):
        pad = tk.Frame(self.left, bg=PANEL)
        pad.pack(fill="both", expand=True, padx=18, pady=18)

        self._section_title(
            pad,
            "Participants",
            "Choose two open ChatGPT conversations. They may belong to different Projects.",
        ).pack(fill="x", pady=(0, 16))

        self.a_select, self.a_name, self.a_badge = self._participant_card(
            pad, "A", A_COLOR, "Chat A"
        )
        self.a_select.bind("<<ComboboxSelected>>", lambda _e: self._selection_changed("A"))

        self.b_select, self.b_name, self.b_badge = self._participant_card(
            pad, "B", B_COLOR, "Chat B"
        )
        self.b_select.bind("<<ComboboxSelected>>", lambda _e: self._selection_changed("B"))

        row = tk.Frame(pad, bg=PANEL)
        row.pack(fill="x", pady=(4, 10))
        self.refresh_button = ttk.Button(
            row,
            text="Refresh tabs",
            style="Secondary.TButton",
            command=self.refresh_tabs,
        )
        self.refresh_button.pack(side="left", fill="x", expand=True)

        self.init_button = ttk.Button(
            pad,
            text="Initialize Both",
            style="Primary.TButton",
            command=self.initialize_both,
        )
        self.init_button.pack(fill="x", pady=(4, 0))

        tk.Label(
            pad,
            text=(
                "Initialization sends each chat its exact Parley A/B activation "
                "command and verifies its reply markers."
            ),
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 8),
            wraplength=270,
            justify="left",
        ).pack(anchor="w", pady=(10, 0))

    def _participant_card(self, parent, label, color, default_name):
        card = tk.Frame(
            parent,
            bg=PANEL_ALT,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        card.pack(fill="x", pady=(0, 12))

        top = tk.Frame(card, bg=PANEL_ALT)
        top.pack(fill="x", padx=12, pady=(11, 6))
        tk.Label(
            top,
            text=f"CHAT {label}",
            bg=PANEL_ALT,
            fg=color,
            font=("Segoe UI Semibold", 10),
        ).pack(side="left")
        badge = tk.Label(
            top,
            text="Not initialized",
            bg=PANEL_ALT,
            fg=MUTED,
            font=("Segoe UI", 8),
        )
        badge.pack(side="right")

        combo = ttk.Combobox(
            card,
            state="readonly",
            style="Parley.TCombobox",
        )
        combo.pack(fill="x", padx=12, pady=(0, 8))

        tk.Label(
            card,
            text="Display name",
            bg=PANEL_ALT,
            fg=MUTED,
            font=("Segoe UI", 8),
        ).pack(anchor="w", padx=12)
        name = tk.Entry(
            card,
            bg=INPUT,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=("Segoe UI", 10),
        )
        name.insert(0, default_name)
        name.pack(fill="x", padx=12, pady=(4, 12), ipady=6)
        return combo, name, badge

    def _build_center(self, parent):
        prompt_panel = tk.Frame(
            parent,
            bg=PANEL,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        prompt_panel.pack(fill="x", pady=(0, 14))

        head = tk.Frame(prompt_panel, bg=PANEL)
        head.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(
            head,
            text="Session Prompt",
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 12),
        ).pack(side="left")

        round_frame = tk.Frame(head, bg=PANEL)
        round_frame.pack(side="right")
        tk.Label(
            round_frame,
            text="Rounds",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(0, 7))
        self.rounds_var = tk.StringVar(value="5")
        self.rounds_entry = tk.Spinbox(
            round_frame,
            from_=1,
            to=999,
            width=5,
            textvariable=self.rounds_var,
            bg=INPUT,
            fg=TEXT,
            buttonbackground=PANEL_ALT,
            insertbackground=TEXT,
            relief="flat",
            justify="center",
            font=("Segoe UI", 10),
        )
        self.rounds_entry.pack(side="left", ipady=4)

        self.prompt_text = tk.Text(
            prompt_panel,
            height=8,
            wrap="word",
            bg=INPUT,
            fg=TEXT,
            insertbackground=TEXT,
            selectbackground="#30466d",
            relief="flat",
            padx=12,
            pady=10,
            font=("Segoe UI", 10),
            undo=True,
        )
        self.prompt_text.pack(fill="x", padx=16, pady=(0, 12))

        action_row = tk.Frame(prompt_panel, bg=PANEL)
        action_row.pack(fill="x", padx=16, pady=(0, 14))
        self.start_button = ttk.Button(
            action_row,
            text="Start Session",
            style="Primary.TButton",
            command=self.start_session,
        )
        self.start_button.pack(side="right")

        transcript_panel = tk.Frame(
            parent,
            bg=PANEL,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        transcript_panel.pack(fill="both", expand=True)

        th = tk.Frame(transcript_panel, bg=PANEL)
        th.pack(fill="x", padx=16, pady=(13, 8))
        tk.Label(
            th,
            text="Conversation",
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 12),
        ).pack(side="left")
        self.transcript_hint = tk.Label(
            th,
            text="Completed turns appear here",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 9),
        )
        self.transcript_hint.pack(side="right")

        wrap = tk.Frame(transcript_panel, bg=PANEL)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.transcript = tk.Text(
            wrap,
            wrap="word",
            bg=INPUT,
            fg=TEXT,
            relief="flat",
            padx=16,
            pady=14,
            font=("Segoe UI", 10),
            state="disabled",
            cursor="arrow",
        )
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.transcript.yview)
        self.transcript.configure(yscrollcommand=scroll.set)
        self.transcript.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.transcript.tag_configure(
            "a_head", foreground=A_COLOR, font=("Segoe UI Semibold", 10),
            spacing1=10, spacing3=4,
        )
        self.transcript.tag_configure(
            "b_head", foreground=B_COLOR, font=("Segoe UI Semibold", 10),
            spacing1=10, spacing3=4,
        )
        self.transcript.tag_configure(
            "human_head", foreground=SUCCESS, font=("Segoe UI Semibold", 9),
            spacing1=8, spacing3=3,
        )
        self.transcript.tag_configure(
            "body", foreground=TEXT, font=("Segoe UI", 10),
            lmargin1=8, lmargin2=8, rmargin=8, spacing3=10,
        )
        self.transcript.tag_configure(
            "system", foreground=MUTED, font=("Segoe UI", 9, "italic"),
            spacing1=6, spacing3=8,
        )

    def _build_right(self):
        pad = tk.Frame(self.right, bg=PANEL)
        pad.pack(fill="both", expand=True, padx=16, pady=18)

        self._section_title(
            pad,
            "Session",
            "Live relay state. Operations have no automatic timeout.",
        ).pack(fill="x", pady=(0, 14))

        status_card = tk.Frame(
            pad,
            bg=PANEL_ALT,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        status_card.pack(fill="x")

        self.state_value = self._status_row(status_card, "State", "Idle")
        self.phase_value = self._status_row(status_card, "Phase", "—")
        self.round_value = self._status_row(status_card, "Round", "0 / 0")
        self.transfer_value = self._status_row(status_card, "Transfers", "0")
        self.elapsed_value = self._status_row(status_card, "Elapsed", "00:00", last=True)

        controls = tk.Frame(pad, bg=PANEL)
        controls.pack(fill="x", pady=(14, 0))

        row1 = tk.Frame(controls, bg=PANEL)
        row1.pack(fill="x")
        self.pause_button = ttk.Button(
            row1, text="Pause", style="Secondary.TButton", command=self.pause_session
        )
        self.pause_button.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.resume_button = ttk.Button(
            row1, text="Resume", style="Secondary.TButton", command=self.resume_session
        )
        self.resume_button.pack(side="left", fill="x", expand=True, padx=(4, 0))

        self.stop_button = ttk.Button(
            controls, text="Stop", style="Danger.TButton", command=self.stop_session
        )
        self.stop_button.pack(fill="x", pady=(8, 0))

        boundary = tk.Frame(
            pad,
            bg=PANEL_ALT,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        boundary.pack(fill="x", pady=(14, 0))
        tk.Label(
            boundary,
            text="ROUND BOUNDARY",
            bg=PANEL_ALT,
            fg=MUTED,
            font=("Segoe UI Semibold", 8),
        ).pack(anchor="w", padx=11, pady=(10, 6))

        ext_row = tk.Frame(boundary, bg=PANEL_ALT)
        ext_row.pack(fill="x", padx=10)
        self.extend_var = tk.StringVar(value="10")
        self.extend_entry = tk.Entry(
            ext_row,
            textvariable=self.extend_var,
            width=7,
            bg=INPUT,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            justify="center",
            font=("Segoe UI", 10),
        )
        self.extend_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.extend_button = ttk.Button(
            ext_row,
            text="Extend",
            style="Secondary.TButton",
            command=self.extend_session,
        )
        self.extend_button.pack(side="left", padx=(7, 0))

        self.finish_button = ttk.Button(
            boundary,
            text="Finish Here",
            style="Secondary.TButton",
            command=self.finish_session,
        )
        self.finish_button.pack(fill="x", padx=10, pady=(8, 5))

        self.reset_button = ttk.Button(
            boundary,
            text="Reset Chats",
            style="Secondary.TButton",
            command=self.reset_chats,
        )
        self.reset_button.pack(fill="x", padx=10, pady=(0, 10))

        tk.Label(
            pad,
            text="Activity",
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 11),
        ).pack(anchor="w", pady=(16, 7))

        self.activity = tk.Text(
            pad,
            height=12,
            wrap="word",
            bg=INPUT,
            fg=MUTED,
            relief="flat",
            padx=9,
            pady=8,
            font=("Cascadia Mono", 8),
            state="disabled",
        )
        self.activity.pack(fill="both", expand=True)

    def _status_row(self, parent, label, value, last=False):
        row = tk.Frame(parent, bg=PANEL_ALT)
        row.pack(fill="x", padx=11, pady=(8 if label == "State" else 3, 9 if last else 3))
        tk.Label(
            row, text=label, bg=PANEL_ALT, fg=MUTED, font=("Segoe UI", 9)
        ).pack(side="left")
        target = tk.Label(
            row, text=value, bg=PANEL_ALT, fg=TEXT, font=("Segoe UI Semibold", 9)
        )
        target.pack(side="right")
        return target

    def _post(self, fn, *args):
        self._ui_queue.put((fn, args))

    def _drain_ui_queue(self):
        try:
            while True:
                fn, args = self._ui_queue.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        self.root.after(80, self._drain_ui_queue)

    def _run_worker(self, target, name):
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        return thread

    def _selected_tab(self, label):
        combo = self.a_select if label == "A" else self.b_select
        return self.tab_by_label.get(combo.get())

    def _selection_changed(self, label):
        self.ready[label] = False
        self._set_protocol_status(label, "Not initialized", MUTED)
        self._update_controls()

    def _set_protocol_status(self, label, text, color):
        badge = self.a_badge if label == "A" else self.b_badge
        badge.configure(text=text, fg=color)

    def _set_connection(self, text, color):
        self.connection_label.configure(text=f"●  {text}", fg=color)

    def _set_busy(self, busy):
        self._busy = bool(busy)
        if not busy:
            self._operation_stop.clear()
        self._update_controls()

    def _update_controls(self):
        active = bool(self.session and self.session.is_alive())
        ready = self.ready["A"] and self.ready["B"]
        tab_a = self._selected_tab("A")
        tab_b = self._selected_tab("B")
        selected = bool(
            tab_a
            and tab_b
            and tab_a.get("id") != tab_b.get("id")
        )

        self.refresh_button.configure(state="disabled" if self._busy or active else "normal")
        self.init_button.configure(
            state="normal" if selected and not self._busy and not active else "disabled"
        )
        self.start_button.configure(
            state="normal" if ready and not self._busy and not active else "disabled"
        )
        self.pause_button.configure(state="normal" if active else "disabled")
        self.resume_button.configure(state="normal" if active else "disabled")
        self.stop_button.configure(
            state="normal" if active or self._busy else "disabled"
        )

        boundary = False
        if active:
            try:
                boundary = bool(self.session.status().get("awaiting_extension"))
            except Exception:
                boundary = False

        self.extend_button.configure(state="normal" if active else "disabled")
        self.finish_button.configure(state="normal" if boundary else "disabled")
        self.reset_button.configure(
            state="normal" if (boundary or (ready and not active and not self._busy)) else "disabled"
        )

    def refresh_tabs(self):
        if self._busy or (self.session and self.session.is_alive()):
            return
        self._set_busy(True)
        self._set_connection("Scanning Chrome…", WARN)
        self._activity("Scanning open ChatGPT tabs")

        def worker():
            try:
                tabs = core.list_tabs()
                if isinstance(tabs, dict):
                    raise RuntimeError(tabs.get("error") or str(tabs))
                filtered = []
                for tab in tabs:
                    url = tab.get("url") or ""
                    if "chatgpt.com/" in url or "chat.openai.com/" in url:
                        filtered.append(tab)
                self._post(self._tabs_loaded, filtered, None)
            except Exception as exc:
                self._post(self._tabs_loaded, [], str(exc))

        self._run_worker(worker, "parley-gui-tabs")

    def _tabs_loaded(self, tabs, error):
        self._set_busy(False)
        if error:
            self._set_connection("Chrome unavailable", DANGER)
            self._activity(f"Tab scan failed: {error}")
            messagebox.showerror("Parley", f"Could not read ChatGPT tabs.\n\n{error}")
            return

        self.tabs = tabs
        self.tab_by_label = {_tab_label(tab): tab for tab in tabs}
        values = list(self.tab_by_label.keys())
        self.a_select.configure(values=values)
        self.b_select.configure(values=values)

        if values and self.a_select.get() not in self.tab_by_label:
            self.a_select.set(values[0])
            self._selection_changed("A")
        if len(values) > 1 and self.b_select.get() not in self.tab_by_label:
            self.b_select.set(values[1])
            self._selection_changed("B")
        elif values and self.b_select.get() not in self.tab_by_label:
            self.b_select.set(values[0])
            self._selection_changed("B")

        self._set_connection(f"Connected · {len(tabs)} ChatGPT tabs", SUCCESS)
        self._activity(f"Found {len(tabs)} ChatGPT tab(s)")
        self._update_controls()

    def initialize_both(self):
        tab_a = self._selected_tab("A")
        tab_b = self._selected_tab("B")
        if not tab_a or not tab_b:
            messagebox.showwarning("Parley", "Select both Chat A and Chat B first.")
            return
        if tab_a.get("id") == tab_b.get("id"):
            messagebox.showwarning("Parley", "Chat A and Chat B must be different tabs.")
            return

        self.ready = {"A": False, "B": False}
        self._init_pending = {"A", "B"}
        self._init_errors = {}
        self._set_protocol_status("A", "Queued", MUTED)
        self._set_protocol_status("B", "Queued", MUTED)
        self._set_busy(True)
        self._operation_stop.clear()
        self._activity(
            "Provisioning and initializing Chat A and Chat B with serial barriers"
        )

        def progress(event):
            self._post(self._protocol_progress, event)

        def worker():
            try:
                result = workflows.initialize_parley_pair(
                    tab_a["id"],
                    tab_b["id"],
                    should_stop=self._operation_stop.is_set,
                    progress=progress,
                )
            except Exception as exc:
                result = {
                    "ok": False,
                    "error": str(exc),
                    "response_complete": False,
                    "stage": "bootstrap",
                }
            self._post(self._protocol_pair_finished, result)

        self._run_worker(worker, "parley-gui-initialize")

    def _protocol_progress(self, event):
        label = event.get("label")
        if label not in ("A", "B"):
            return

        stage = event.get("stage")
        status = event.get("status")

        if stage == "preflight" and status == "already_active":
            self._set_protocol_status(label, "Ready", SUCCESS)
            self._activity(f"Chat {label} protocol already active")
        elif stage == "provision":
            self._set_protocol_status(label, "Uploading protocol…", WARN)
            self._activity(
                f"Chat {label} uploading {event.get('filename', 'protocol file')}"
            )
        elif stage == "attachment_stabilizing":
            seconds = event.get("seconds", 0)
            self._set_protocol_status(label, "Waiting for upload…", WARN)
            self._activity(
                f"Chat {label} allowing attachment to finish uploading "
                f"({seconds:g}s minimum)"
            )
        elif stage == "protocol_ack":
            self._set_protocol_status(label, "Verifying protocol…", WARN)
            self._activity(f"Chat {label} waiting for protocol receipt")
        elif stage == "protocol_ready":
            self._set_protocol_status(label, "Protocol ready", SUCCESS)
            self._activity(f"Chat {label} protocol file verified")
        elif stage == "activation":
            self._set_protocol_status(label, "Initializing…", WARN)
            self._activity(f"Chat {label} activating protocol")
        elif stage == "ready":
            self._init_pending.discard(label)
            self._set_protocol_status(label, "Ready", SUCCESS)
            self._activity(f"Chat {label} protocol ready")

        self._update_controls()

    def _protocol_pair_finished(self, result):
        participants = (
            result.get("participants", {})
            if isinstance(result, dict)
            else {}
        )

        for label in ("A", "B"):
            participant = participants.get(label, {})
            activation = participant.get("activation")
            if isinstance(activation, dict):
                response_text = activation.get("response_text")
                if response_text:
                    name_var = self.a_name if label == "A" else self.b_name
                    name = name_var.get().strip() or f"Chat {label}"
                    self._append_transcript(
                        label,
                        f"{name} · Initialization",
                        _display_reply(response_text, label),
                    )

            self.ready[label] = bool(
                participant.get("already_active")
                or (
                    isinstance(activation, dict)
                    and not activation.get("error")
                    and activation.get("response_complete")
                )
            )

        self._init_pending.clear()
        self._set_busy(False)

        if isinstance(result, dict) and result.get("ok"):
            self.ready = {"A": True, "B": True}
            self._set_protocol_status("A", "Ready", SUCCESS)
            self._set_protocol_status("B", "Ready", SUCCESS)
            self._activity("Both protocols provisioned and verified")
            self._set_phase("Ready")
            self._update_controls()
            return

        error = result.get("error") if isinstance(result, dict) else str(result)
        participant = (
            result.get("participant")
            if isinstance(result, dict)
            else None
        )
        stage = result.get("stage") if isinstance(result, dict) else "bootstrap"

        if error == "chatgpt_wait_stopped":
            for label in ("A", "B"):
                if not self.ready[label]:
                    self._set_protocol_status(label, "Not initialized", MUTED)
            self._activity("Protocol initialization cancelled")
        else:
            if participant in ("A", "B"):
                self._set_protocol_status(participant, "Failed", DANGER)
                self._init_errors[participant] = error or "unknown error"
            self._activity(
                f"Protocol initialization failed at {stage}: {error}"
            )
            messagebox.showerror(
                "Protocol initialization failed",
                (
                    f"Chat {participant or '?'} failed during {stage}.\n\n"
                    f"{error or 'Unknown error'}"
                ),
            )

        self._update_controls()

    def _rounds(self):
        try:
            rounds = int(self.rounds_var.get())
        except ValueError:
            return None
        return rounds if rounds > 0 else None

    def start_session(self):
        if not (self.ready["A"] and self.ready["B"]):
            messagebox.showwarning("Parley", "Initialize both chats before starting.")
            return

        tab_a = self._selected_tab("A")
        tab_b = self._selected_tab("B")
        rounds = self._rounds()
        prompt = self.prompt_text.get("1.0", "end-1c").strip()
        if not tab_a or not tab_b or tab_a["id"] == tab_b["id"]:
            messagebox.showwarning("Parley", "Select two different ChatGPT tabs.")
            return
        if rounds is None:
            messagebox.showwarning("Parley", "Rounds must be a positive whole number.")
            return
        if not prompt:
            messagebox.showwarning("Parley", "Enter a session prompt.")
            return

        self._clear_transcript()
        self._append_transcript("H", "Session brief", prompt)
        self._set_busy(True)
        self._operation_stop.clear()
        self._set_phase("Sending prompt to Chat A")
        self._activity("Sending initial prompt to Chat A")

        def worker():
            result = workflows.send_and_wait(
                tab_a["id"],
                prompt,
                wait_timeout_ms=None,
                expected_reply_prefix=TEST_A_REPLY_PREFIX,
                expected_reply_suffix=TEST_A_REPLY_SUFFIX,
                should_stop=self._operation_stop.is_set,
            )
            if not _result_ok(result):
                self._post(self._start_failed, result)
                return

            text = (result.get("response_text") or "").strip()
            if text == RESET_CHAT_COMMAND:
                self._post(self._start_failed, {
                    "error": "Chat A returned RESET CHAT instead of a starting reply."
                })
                return

            session = RelaySession(
                workflows.bridge,
                tab_a["id"],
                tab_b["id"],
                rounds,
                include_text=True,
                initial_context=prompt,
            )
            self._post(self._start_relay, session, text)

        self._run_worker(worker, "parley-gui-start")

    def _start_relay(self, session, initial_text):
        self.session = session
        self._session_done_seen = False
        self._seen_transfers = 0
        self._seen_events = 0
        self._set_busy(False)
        self._append_transcript(
            "A",
            self.a_name.get().strip() or "Chat A",
            _display_reply(initial_text, "A"),
        )
        session.start()
        self._set_phase("Relay started")
        self._activity("Relay started")
        self._update_controls()

    def _start_failed(self, result):
        self._set_busy(False)
        error = result.get("error") if isinstance(result, dict) else str(result)
        if error == "chatgpt_wait_stopped":
            self._set_phase("Ready")
            self._activity("Session start cancelled")
            return

        self._set_phase("Start failed")
        self._activity(f"Start failed: {error}")
        messagebox.showerror("Could not start session", error or str(result))

    def pause_session(self):
        if self.session and self.session.is_alive():
            self.session.pause()
            self._activity("Pause requested")
            self._update_controls()

    def resume_session(self):
        if self.session and self.session.is_alive():
            self.session.resume()
            self._activity("Resume requested")
            self._update_controls()

    def stop_session(self):
        if self._busy:
            self._operation_stop.set()
            self._activity("Stop requested for current browser operation")
        if self.session and self.session.is_alive():
            self.session.stop()
            self._activity("Stop requested for relay")
        self._update_controls()

    def extend_session(self):
        if not self.session or not self.session.is_alive():
            return
        try:
            new_total = int(self.extend_var.get())
            updated = self.session.extend_rounds(new_total)
        except Exception as exc:
            messagebox.showwarning("Parley", str(exc))
            return
        self._activity(f"Round limit extended to {updated}")
        self._update_controls()

    def finish_session(self):
        if self.session and self.session.is_alive():
            self.session.finish_at_round_limit()
            self._activity("Finishing at current round limit")

    def reset_chats(self):
        if self.session and self.session.is_alive():
            status = self.session.status()
            if not status.get("awaiting_extension"):
                messagebox.showinfo(
                    "Reset Chats",
                    "A coordinated UI reset is available at a round boundary. "
                    "You can stop now, or wait for the current round to finish.",
                )
                return
            self.session.request_reset("A")
            self._activity("Coordinated RESET CHAT requested from A")
            return

        if self._busy:
            return

        tab_a = self._selected_tab("A")
        tab_b = self._selected_tab("B")
        if not tab_a or not tab_b or not (self.ready["A"] and self.ready["B"]):
            return

        self._set_busy(True)
        self._operation_stop.clear()
        self._activity("Resetting both chats")

        def worker():
            for label, tab, prefix, suffix in (
                ("A", tab_a, TEST_A_REPLY_PREFIX, TEST_A_REPLY_SUFFIX),
                ("B", tab_b, TEST_B_REPLY_PREFIX, TEST_B_REPLY_SUFFIX),
            ):
                result = workflows.send_and_wait(
                    tab["id"],
                    RESET_CHAT_COMMAND,
                    wait_timeout_ms=None,
                    expected_reply_prefix=prefix,
                    expected_reply_suffix=suffix,
                    should_stop=self._operation_stop.is_set,
                )
                text = (
                    (result.get("response_text") or "").strip()
                    if isinstance(result, dict)
                    else ""
                )
                if not _result_ok(result) or text != RESET_CHAT_COMMAND:
                    self._post(self._reset_failed, label, result)
                    return
            self._post(self._reset_done)

        self._run_worker(worker, "parley-gui-reset")

    def _reset_done(self):
        self._set_busy(False)
        self._clear_transcript()
        self._append_system("Chats reset. Ready for a new session.")
        self._set_phase("Ready")
        self._activity("RESET CHAT acknowledged by A and B")
        self._update_controls()

    def _reset_failed(self, label, result):
        self._set_busy(False)
        error = result.get("error") if isinstance(result, dict) else str(result)
        if error == "chatgpt_wait_stopped":
            self._activity("Reset cancelled")
            return

        self._activity(f"Reset failed at Chat {label}: {error}")
        messagebox.showerror("Reset failed", f"Chat {label}: {error or result}")

    def _poll(self):
        try:
            self._poll_session()
        finally:
            self.root.after(250, self._poll)

    def _poll_session(self):
        session = self.session
        if not session:
            self._tick_elapsed()
            self._update_controls()
            return

        status = session.status()
        state = status.get("state") or "IDLE"
        control = status.get("control") or "running"
        state_text = "Paused" if control == "paused" else state.replace("_", " ").title()
        self.state_value.configure(text=state_text)

        rounds_done = int(status.get("rounds_completed", 0) or 0)
        rounds_total = int(status.get("rounds_requested", 0) or 0)
        self.round_value.configure(text=f"{rounds_done} / {rounds_total}")
        self.transfer_value.configure(text=str(status.get("transfers_completed", 0)))

        phase = self._phase_for_status(status)
        if phase != self._phase:
            self._set_phase(phase)

        transfers = session.transfers()
        while self._seen_transfers < len(transfers):
            item = transfers[self._seen_transfers]
            self._seen_transfers += 1
            direction = item.get("direction")
            if direction == "A->B":
                label = "B"
                name = self.b_name.get().strip() or "Chat B"
            else:
                label = "A"
                name = self.a_name.get().strip() or "Chat A"
            text = _display_reply(item.get("response_text"), label)
            self._append_transcript(label, name, text)

        events = session.events()
        while self._seen_events < len(events):
            event = events[self._seen_events]
            self._seen_events += 1
            kind = event.get("event")
            if kind in (
                "relay_round_limit_reached",
                "relay_round_limit_extended",
                "relay_reset_requested",
                "relay_reset_propagated",
                "relay_error",
                "relay_stopped",
            ):
                self._activity(self._event_summary(event))

        if not session.is_alive() and not self._session_done_seen:
            self._session_done_seen = True
            result = session.result
            if session.exception is not None:
                self._set_phase("Error")
                self._activity(f"Relay worker failed: {session.exception}")
            elif isinstance(result, dict):
                status_text = result.get("status", "finished")
                self._set_phase(status_text.title())
                if result.get("error"):
                    self._activity(
                        f"Relay error: {result.get('error')} · {result.get('stage', '')}"
                    )
                elif result.get("reset_requested"):
                    self._append_system("RESET CHAT coordinated. Restart point: Chat A.")
                else:
                    self._append_system(
                        f"Session {status_text}. "
                        f"{result.get('rounds_completed', 0)} round(s) completed."
                    )
            self._update_controls()

        self._tick_elapsed()
        self._update_controls()

    def _phase_for_status(self, status):
        if status.get("awaiting_extension"):
            return "Round limit reached"
        if status.get("control") == "paused":
            return "Paused"

        state = status.get("state")
        if state == "PREPARE":
            return "Preparing session"
        if state == "READ_A":
            return "Reading Chat A"
        if state == "TRANSFER_A_TO_B":
            return "A → B"
        if state == "TRANSFER_B_TO_A":
            return "B → A"
        if state == "COMPLETE":
            return "Complete"
        if state == "ERROR":
            return "Error"
        if state == "STOPPED":
            return "Stopped"
        return state.replace("_", " ").title() if state else "Idle"

    def _set_phase(self, phase):
        self._phase = phase
        self._phase_started = time.monotonic()
        self.phase_value.configure(text=phase)

    def _tick_elapsed(self):
        seconds = max(0, int(time.monotonic() - self._phase_started))
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            text = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        else:
            text = f"{minutes:02d}:{seconds:02d}"
        self.elapsed_value.configure(text=text)

    def _event_summary(self, event):
        kind = event.get("event", "event")
        if kind == "relay_round_limit_reached":
            return f"Round limit reached at {event.get('round_limit')}"
        if kind == "relay_round_limit_extended":
            return f"Round limit extended to {event.get('round_limit')}"
        if kind == "relay_reset_requested":
            return f"Reset requested by Chat {event.get('chat')}"
        if kind == "relay_reset_propagated":
            return (
                f"Reset propagated {event.get('source_chat')} → "
                f"{event.get('destination_chat')}"
            )
        if kind == "relay_error":
            return f"Relay error: {event.get('error')} ({event.get('stage')})"
        if kind == "relay_stopped":
            return f"Relay stopped at {event.get('stage')}"
        return kind

    def _append_transcript(self, label, name, text):
        self.transcript.configure(state="normal")
        if label == "A":
            tag = "a_head"
        elif label == "B":
            tag = "b_head"
        else:
            tag = "human_head"
        self.transcript.insert("end", f"{name}\n", tag)
        self.transcript.insert("end", f"{text.strip()}\n", "body")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _append_system(self, text):
        self.transcript.configure(state="normal")
        self.transcript.insert("end", f"{text}\n", "system")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _clear_transcript(self):
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")

    def _activity(self, text):
        timestamp = time.strftime("%H:%M:%S")
        self.activity.configure(state="normal")
        self.activity.insert("end", f"{timestamp}  {text}\n")
        self.activity.configure(state="disabled")
        self.activity.see("end")


def launch():
    os.environ.setdefault("PARLEY_CONNECTION_MODE", "live")
    root = tk.Tk()
    ParleyApp(root)
    root.mainloop()
    return 0
