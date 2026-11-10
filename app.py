#!/usr/bin/env python3
"""
app.py - Bicing Planner desktop app (CustomTkinter + OpenStreetMap map).

    uv run python app.py

Layout: plain-language bar on top (Jev) with the language menu, form on the left (Plan trip /
Nearby tabs, data status), map on the right with the results under it. Network work (live data,
address search, Jev) runs in background threads so the window never freezes.

Switching language rebuilds the window: the form is captured in language-neutral codes first,
then restored and the last result is drawn again in the new language.
"""

import csv
import queue
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox
from urllib.error import URLError

import customtkinter as ctk
import tkintermapview

import app_config
import app_paths
import citybikes
import geocode
import i18n
import planner
from app_paths import HISTORY_CSV, ICON_FILE, LIVE_CSV
from i18n import t

STALE_AFTER_MIN = 10           # ask to update the snapshot when live data is older than this
BCN_CENTER = (41.3874, 2.1686)
MAP_CANDIDATES = 5             # stations per trip end drawn on the map
EXPORT_CANDIDATES = 8          # stations per trip end written by "Save as CSV"
TEXT_OPTIONS = 4               # extra stations per trip end listed under "Other options"
STATUS_REFRESH_MS = 30_000

COLOR = {
    "good": "#2e9e44", "mid": "#e69500", "bad": "#d0342c", "offline": "#8a8a8a",
    "place": "#1f6aa5", "pick": "#0b7a35", "drop": "#7b2cbf", "ride": "#1f6aa5", "walk": "#9e9e9e",
}
STATUS_COLOR = {"ok": COLOR["good"], "low": COLOR["mid"], "none": COLOR["bad"], "offline": COLOR["offline"]}
TIME_INPUT_RE = re.compile(r"^\s*(\d{1,2})(?:[:.h](\d{2}))?\s*h?\s*$")
BASIS_RE = re.compile(r"^(weekday|weekend) (\d{2})h(±1)?$")

# language-neutral codes <-> interface text keys
WHEN_KEYS = {"now": "now", "depart_at": "leave_at", "arrive_by": "arrive_by"}
BIKE_KEYS = {"either": "any_bike", "ebike": "ebike", "mechanical": "mechanical"}
NEED_KEYS = {"bikes": "need_bikes", "ebike": "need_ebike", "mechanical": "need_mechanical", "docks": "need_docks"}


def code_of(mapping: dict, label: str, default: str) -> str:
    """Interface text (current language) -> code, using a {code: text key} mapping."""
    return next((code for code, key in mapping.items() if t(key) == label), default)


def parse_hhmm(text: str):
    m = TIME_INPUT_RE.match(text or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    return (h, mi) if h <= 23 and mi <= 59 else None


def reliability_color(row) -> str:
    if not row.get("online", True):
        return COLOR["offline"]
    p = row["reliability"]
    return COLOR["good"] if p >= 0.7 else COLOR["mid"] if p >= 0.45 else COLOR["bad"]


def basis_text(basis: str) -> str:
    """planner's basis labels ('weekday 19h', 'weekday 19h±1', 'all history', 'live only') -> text."""
    if basis == "live only":
        return t("basis_live")
    if basis == "all history":
        return t("basis_all")
    m = BASIS_RE.match(basis)
    if m:
        return t("basis_hour_near" if m.group(3) else "basis_hour",
                 day=t(f"daytype_{m.group(1)}"), h=int(m.group(2)))
    return basis


# --- background work -------------------------------------------------------

class Worker:
    """Runs functions in threads and delivers results back on the Tk main thread."""

    def __init__(self, root, on_error):
        self.root, self.on_error = root, on_error
        self.q: queue.Queue = queue.Queue()
        root.after(100, self._poll)

    def run(self, fn, on_done, on_error=None):
        def target():
            try:
                self.q.put((on_done, fn(), None, on_error))
            except Exception as e:  # delivered to the UI thread
                self.q.put((None, None, e, on_error))
        threading.Thread(target=target, daemon=True).start()

    def _poll(self):
        try:
            while True:
                on_done, result, err, on_error = self.q.get_nowait()
                if err is not None:
                    (on_error or self.on_error)(err)
                elif on_done:
                    on_done(result)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)


class DataStore:
    """Live stations and history, re-read only when the files change."""

    def __init__(self):
        self._lock = threading.Lock()
        self._live = (None, {})
        self._hist = (None, None, 0)

    def live_age_min(self):
        try:
            return (time.time() - LIVE_CSV.stat().st_mtime) / 60
        except OSError:
            return None

    def stations(self):
        with self._lock:
            mtime = LIVE_CSV.stat().st_mtime
            if self._live[0] != mtime:
                self._live = (mtime, planner.load_live(str(LIVE_CSV)))
            return self._live[1]

    def history(self):
        with self._lock:
            try:
                mtime = HISTORY_CSV.stat().st_mtime
            except OSError:   # no history file yet
                hist, n = planner.read_history(str(HISTORY_CSV))
                self._hist = (-1, hist, n)
                return hist, n
            if self._hist[0] != mtime:
                hist, n = planner.read_history(str(HISTORY_CSV))
                self._hist = (mtime, hist, n)
            return self._hist[1], self._hist[2]

    def snapshot_count(self):
        return self._hist[2] if self._hist[0] is not None else None

    def update(self):
        return citybikes.update_snapshot(LIVE_CSV, HISTORY_CSV)


# --- address field ---------------------------------------------------------

class PlaceField(ctk.CTkFrame):
    """Type an address, press Enter or Find; the best match is used and a dropdown offers the others."""

    def __init__(self, master, app, label, on_change):
        super().__init__(master, fg_color="transparent")
        self.app, self.on_change = app, on_change
        self.places: list[geocode.Place] = []
        self.place: geocode.Place | None = None
        self._searched: str | None = None
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text=label, anchor="w", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(2, 2))
        self.entry = ctk.CTkEntry(self, placeholder_text=t("place_placeholder"))
        self.entry.grid(row=1, column=0, sticky="ew")
        self.entry.bind("<Return>", lambda e: self.search())
        self.btn = ctk.CTkButton(self, text=t("find"), width=64, command=self.search)
        self.btn.grid(row=1, column=1, padx=(6, 0))
        self.menu = ctk.CTkOptionMenu(self, values=[""], command=self._pick, dynamic_resizing=False,
                                      height=26, width=300, fg_color=("gray85", "gray25"),
                                      button_color=("gray75", "gray30"), text_color=("gray10", "gray90"))
        self.menu.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.menu.grid_remove()
        self.note = ctk.CTkLabel(self, text="", anchor="w", justify="left", wraplength=320,
                                 text_color=("gray35", "gray65"), font=ctk.CTkFont(size=11))
        self.note.grid(row=3, column=0, columnspan=2, sticky="w")
        self.note.grid_remove()

    def text(self) -> str:
        return self.entry.get().strip()

    def resolved(self) -> bool:
        return self.place is not None and self._searched == self.text()

    def set_text(self, text: str) -> None:
        self.entry.delete(0, "end")
        if text:
            self.entry.insert(0, text)
        self.place, self.places, self._searched = None, [], None
        self.menu.grid_remove()
        self._set_note("")
        self.on_change(self)

    def get_state(self) -> dict:
        index = self.places.index(self.place) if self.place in self.places else None
        return {"text": self.text(), "places": self.places, "index": index, "searched": self._searched}

    def restore(self, state: dict) -> None:
        """Put back a captured state without searching again (used after a language switch)."""
        self.entry.delete(0, "end")
        if state["text"]:
            self.entry.insert(0, state["text"])
        self.places, self._searched = state["places"], state["searched"]
        if state["index"] is not None and self.places:
            self._show_menu(state["index"])

    def _show_menu(self, index: int) -> None:
        labels = [f"{i + 1}. {p.label}" for i, p in enumerate(self.places)]
        self.menu.configure(values=labels)
        self.menu.set(labels[index])
        self.menu.grid()
        self.place = self.places[index]

    def _set_note(self, text: str) -> None:
        self.note.configure(text=text)
        if text:
            self.note.grid()
        else:
            self.note.grid_remove()

    def search(self, then=None):
        query = self.text()
        if not query:
            self._set_note(t("type_address"))
            if then:
                then(False)
            return
        self._set_note(t("searching"))
        self.btn.configure(state="disabled")
        lang = i18n.current()
        self.app.worker.run(lambda: geocode.search(query, lang=lang),
                            lambda res: self._found(query, res, then),
                            lambda e: self._failed(e, then))

    def _found(self, query, places, then):
        if not self.winfo_exists():   # window was rebuilt (language switch) meanwhile
            return
        self.btn.configure(state="normal")
        self.places, self._searched = places, query
        if not places:
            self.place = None
            self.menu.grid_remove()
            self._set_note(t("no_match"))
        else:
            self._show_menu(0)
            self._set_note("")
        self.on_change(self)
        if then:
            then(bool(places))

    def _failed(self, err, then):
        if not self.winfo_exists():
            return
        self.btn.configure(state="normal")
        self._set_note("")
        self.app.show_error(err)
        if then:
            then(False)

    def _pick(self, label):
        self.place = self.places[int(label.split(".", 1)[0]) - 1]
        self.on_change(self)


# --- main window -----------------------------------------------------------

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.geometry("1280x820")
        self.minsize(1060, 700)
        if ICON_FILE.exists():
            self.iconbitmap(str(ICON_FILE))
        self.worker = Worker(self, self.show_error)
        self.data = DataStore()
        self.busy = False
        self.export_rows: list[dict] = []
        self.export_kind = ""
        self._last_result = None          # ("plan" | "nearby", result) for re-drawing
        self._place_markers: dict = {}
        self.bold = ctk.CTkFont(weight="bold")
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_ui()
        self.worker.run(self.data.history, lambda _: self.refresh_status())   # count snapshots
        self.after(STATUS_REFRESH_MS, self._tick)

    def _build_ui(self):
        self.title(t("app_title"))
        self._build_askbar()
        self._build_sidebar()
        self._build_main()
        self.refresh_key_state()
        self.refresh_status()

    # ---- layout ----

    def _build_askbar(self):
        bar = self.askbar = ctk.CTkFrame(self)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 0))
        bar.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(bar, text=t("ask_label"), font=self.bold).grid(row=0, column=0, padx=(12, 6), pady=(10, 4))
        self.ask_entry = ctk.CTkEntry(bar, placeholder_text=t("ask_placeholder"), height=34)
        self.ask_entry.grid(row=0, column=1, sticky="ew", pady=(10, 4))
        self.ask_entry.bind("<Return>", lambda e: self.on_ask())
        self.ask_btn = ctk.CTkButton(bar, text=t("ask_button"), width=130, height=34, command=self.on_ask)
        self.ask_btn.grid(row=0, column=2, padx=(10, 6), pady=(10, 4))
        names = list(i18n.LANGUAGE_NAMES.values())
        self.lang_menu = ctk.CTkOptionMenu(bar, values=names, width=110, height=34, command=self.on_language)
        self.lang_menu.set(i18n.LANGUAGE_NAMES[i18n.current()])
        self.lang_menu.grid(row=0, column=3, padx=(0, 10), pady=(10, 4))
        self.jev_status = ctk.CTkLabel(bar, text="", anchor="w", justify="left", wraplength=1150,
                                       text_color=("gray30", "gray70"))
        self.jev_status.grid(row=1, column=0, columnspan=4, sticky="ew", padx=12, pady=(0, 8))

    def _build_sidebar(self):
        side = self.sidebar = ctk.CTkFrame(self)
        side.grid(row=1, column=0, sticky="ns", padx=10, pady=10)
        side.grid_rowconfigure(0, weight=1)
        self.tabs = ctk.CTkTabview(side, width=370)
        self.tabs.grid(row=0, column=0, sticky="nsew", padx=8, pady=(0, 6))
        self._build_trip_tab(self.tabs.add(t("tab_trip")))
        self._build_nearby_tab(self.tabs.add(t("tab_nearby")))

        status = ctk.CTkFrame(side, fg_color="transparent")
        status.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 12))
        status.grid_columnconfigure((0, 1), weight=1)
        self.live_label = ctk.CTkLabel(status, text="", anchor="w")
        self.live_label.grid(row=0, column=0, columnspan=2, sticky="w")
        self.hist_label = ctk.CTkLabel(status, text="", anchor="w", text_color=("gray35", "gray65"))
        self.hist_label.grid(row=1, column=0, columnspan=2, sticky="w")
        self.update_btn = ctk.CTkButton(status, text=t("update_now"), command=self.on_update)
        self.update_btn.grid(row=2, column=0, sticky="ew", padx=(0, 4), pady=(6, 0))
        ctk.CTkButton(status, text=t("settings"), fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=self.open_settings).grid(
            row=2, column=1, sticky="ew", padx=(4, 0), pady=(6, 0))
        self.busy_label = ctk.CTkLabel(status, text="", anchor="w", justify="left", wraplength=340,
                                       text_color=COLOR["place"])
        self.busy_label.grid(row=3, column=0, columnspan=2, sticky="w")

    def _section(self, tab, row, text):
        ctk.CTkLabel(tab, text=text, anchor="w", font=self.bold).grid(row=row, column=0, sticky="w", pady=(8, 0))

    def _build_trip_tab(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        self.from_field = PlaceField(tab, self, t("from"), self.on_place_changed)
        self.from_field.grid(row=0, column=0, sticky="ew")
        self.to_field = PlaceField(tab, self, t("to"), self.on_place_changed)
        self.to_field.grid(row=1, column=0, sticky="ew")

        self._section(tab, 2, t("when"))
        self.when_seg = ctk.CTkSegmentedButton(tab, values=[t(k) for k in WHEN_KEYS.values()],
                                               command=self._when_changed)
        self.when_seg.grid(row=3, column=0, sticky="ew")
        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.day_menu = ctk.CTkOptionMenu(row, values=[t("today"), t("tomorrow")], width=120)
        self.day_menu.grid(row=0, column=0)
        self.time_entry = ctk.CTkEntry(row, width=80, placeholder_text="09:00")
        self.time_entry.grid(row=0, column=1, padx=(8, 0))
        self.when_seg.set(t("now"))
        self._when_changed(t("now"))

        self._section(tab, 5, t("bike"))
        self.bike_seg = ctk.CTkSegmentedButton(tab, values=[t(k) for k in BIKE_KEYS.values()])
        self.bike_seg.grid(row=6, column=0, sticky="ew")
        self.bike_seg.set(t("any_bike"))

        self._section(tab, 7, t("walk"))
        self.walk_slider, self.walk_value = self._slider(tab, 8, planner.DEFAULT_RADIUS_M)

        self.plan_btn = ctk.CTkButton(tab, text=t("plan_button"), height=40, font=self.bold, command=self.on_plan)
        self.plan_btn.grid(row=9, column=0, sticky="ew", pady=(14, 4))

    def _build_nearby_tab(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        self.near_field = PlaceField(tab, self, t("place"), self.on_place_changed)
        self.near_field.grid(row=0, column=0, sticky="ew")
        self._section(tab, 1, t("looking_for"))
        self.need_seg = ctk.CTkSegmentedButton(tab, values=[t(k) for k in NEED_KEYS.values()])
        self.need_seg.grid(row=2, column=0, sticky="ew")
        self.need_seg.set(t("need_bikes"))
        self._section(tab, 3, t("radius"))
        self.radius_slider, self.radius_value = self._slider(tab, 4, planner.DEFAULT_RADIUS_M)
        self.near_btn = ctk.CTkButton(tab, text=t("nearby_button"), height=40, font=self.bold,
                                      command=self.on_nearby)
        self.near_btn.grid(row=5, column=0, sticky="ew", pady=(14, 4))

    def _slider(self, tab, row, value):
        frame = ctk.CTkFrame(tab, fg_color="transparent")
        frame.grid(row=row, column=0, sticky="ew")
        frame.grid_columnconfigure(0, weight=1)
        label = ctk.CTkLabel(frame, text=t("meters", m=value), width=60, anchor="e")
        slider = ctk.CTkSlider(frame, from_=200, to=planner.MAX_RADIUS_M, number_of_steps=21,
                               command=lambda v: label.configure(text=t("meters", m=int(v))))
        slider.set(value)
        slider.grid(row=0, column=0, sticky="ew")
        label.grid(row=0, column=1, padx=(8, 0))
        return slider, label

    def _set_slider(self, slider, label, value):
        slider.set(value)
        label.configure(text=t("meters", m=int(slider.get())))

    def _build_main(self):
        main = self.mainframe = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=1, column=1, sticky="nsew", padx=(0, 10), pady=10)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(0, weight=5)    # map gets most of the height
        main.grid_rowconfigure(2, weight=3)
        # corner_radius=0: rounded corners would need a solid parent colour, and `main` is transparent
        self.map = tkintermapview.TkinterMapView(main, corner_radius=0)
        self.map.grid(row=0, column=0, sticky="nsew")
        self.map.set_position(*BCN_CENTER)
        self.map.set_zoom(13)

        head = ctk.CTkFrame(main, fg_color="transparent")
        head.grid(row=1, column=0, sticky="ew", pady=(8, 4))
        head.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(head, text=t("results_title"), font=ctk.CTkFont(size=15, weight="bold")).grid(row=0, column=0)
        ctk.CTkLabel(head, text=t("legend"), text_color=("gray35", "gray65"), justify="left",
                     wraplength=560, font=ctk.CTkFont(size=11)).grid(row=0, column=1, sticky="w", padx=12)
        self.save_btn = ctk.CTkButton(head, text=t("save_csv"), width=130, command=self.on_save_csv,
                                      state="normal" if self.export_rows else "disabled")
        self.save_btn.grid(row=0, column=2)

        self.results = ctk.CTkTextbox(main, wrap="word", font=ctk.CTkFont(family="Consolas", size=13))
        self.results.grid(row=2, column=0, sticky="nsew")
        # mid-tone colours: readable on the light and the dark (Windows dark mode) text background
        self.results.tag_config("h1", foreground="#3b8ed0")
        self.results.tag_config("pick", foreground="#2fa84f")
        self.results.tag_config("drop", foreground="#a46ad8")
        self.results.tag_config("warn", foreground="#e5484d")
        self.results.tag_config("muted", foreground="gray55")
        self.write_results([(t("results_empty"), "muted")])

    # ---- language ----

    def on_language(self, name: str):
        code = next(c for c, n in i18n.LANGUAGE_NAMES.items() if n == name)
        if code == i18n.current() or self.busy:
            self.lang_menu.set(i18n.LANGUAGE_NAMES[i18n.current()])
            return
        state = self._capture()                 # in the old language
        i18n.set_language(code)
        app_config.set_language(code)
        for frame in (self.askbar, self.sidebar, self.mainframe):
            frame.destroy()
        self._place_markers.clear()
        self._build_ui()
        self._restore(state)

    def _capture(self) -> dict:
        return {
            "tab": "nearby" if self.tabs.get() == t("tab_nearby") else "trip",
            "ask": self.ask_entry.get(),
            "from": self.from_field.get_state(), "to": self.to_field.get_state(),
            "near": self.near_field.get_state(),
            "when": code_of(WHEN_KEYS, self.when_seg.get(), "now"),
            "day": "tomorrow" if self.day_menu.get() == t("tomorrow") else "today",
            "time": self.time_entry.get(),
            "bike": code_of(BIKE_KEYS, self.bike_seg.get(), "either"),
            "walk": self.walk_slider.get(),
            "need": code_of(NEED_KEYS, self.need_seg.get(), "bikes"),
            "radius": self.radius_slider.get(),
            "map": (self.map.get_position(), self.map.zoom),
        }

    def _restore(self, s: dict):
        if s["ask"]:
            self.ask_entry.insert(0, s["ask"])
        for field, key in ((self.from_field, "from"), (self.to_field, "to"), (self.near_field, "near")):
            field.restore(s[key])
        self.time_entry.configure(state="normal")
        self.time_entry.delete(0, "end")
        if s["time"]:
            self.time_entry.insert(0, s["time"])
        self.day_menu.set(t(s["day"]))
        mode = t(WHEN_KEYS[s["when"]])
        self.when_seg.set(mode)
        self._when_changed(mode)
        self.bike_seg.set(t(BIKE_KEYS[s["bike"]]))
        self._set_slider(self.walk_slider, self.walk_value, s["walk"])
        self.need_seg.set(t(NEED_KEYS[s["need"]]))
        self._set_slider(self.radius_slider, self.radius_value, s["radius"])
        self.select_tab(t("tab_nearby") if s["tab"] == "nearby" else t("tab_trip"))
        if self._last_result:
            kind, result = self._last_result
            (self._show_plan if kind == "plan" else self._show_nearby)(result)
        else:
            (lat, lon), zoom = s["map"]
            self.map.set_position(lat, lon)
            self.map.set_zoom(zoom)
            for field in (self.from_field, self.to_field, self.near_field):
                if field.place:
                    self.on_place_changed(field, fit=False)

    # ---- small helpers ----

    def select_tab(self, name: str):
        """CTkTabview.set() hides the other tabs 100 ms later; calling it again for a different
        tab inside that window hides the new one too. Only switch when it's actually a change."""
        if self.tabs.get() != name:
            self.tabs.set(name)

    def write_results(self, parts):
        self.results.configure(state="normal")
        self.results.delete("1.0", "end")
        for text, tag in parts:
            if tag:
                self.results.insert("end", text, tag)
            else:
                self.results.insert("end", text)
        self.results.configure(state="disabled")

    def set_busy(self, busy: bool, message: str = ""):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.plan_btn, self.near_btn, self.update_btn, self.ask_btn, self.lang_menu):
            b.configure(state=state)
        self.busy_label.configure(text=message if busy else "")
        self.configure(cursor="watch" if busy else "")

    def _age_text(self, minutes):
        if minutes < 1:
            return t("age_just_now")
        if minutes < 90:
            return t("age_min", n=int(minutes))
        if minutes < 48 * 60:
            return t("age_hours", n=int(minutes // 60))
        return t("age_days", n=int(minutes // 1440))

    def refresh_status(self):
        age = self.data.live_age_min()
        self.live_label.configure(text=t("live_none") if age is None else t("live_age", age=self._age_text(age)))
        n = self.data.snapshot_count()
        self.hist_label.configure(text=t("history_loading") if n is None else t("history_count", n=n))

    def _tick(self):
        self.refresh_status()
        self.after(STATUS_REFRESH_MS, self._tick)

    def refresh_key_state(self):
        key, _ = app_config.api_key()
        self.ask_btn.configure(text=t("ask_button") if key else t("ask_set_key"))
        self.jev_status.configure(text=t("ask_hint") if key else t("ask_hint_no_key"))

    def _friendly(self, err) -> str:
        if type(err).__name__ == "TypeSafeAuthenticationError":
            return t("err_key")
        if isinstance(err, (URLError, ConnectionError, TimeoutError)) or "Connection" in type(err).__name__:
            return t("err_network", detail=str(err))
        return f"{type(err).__name__}: {err}"

    def show_error(self, err):
        self.set_busy(False)
        messagebox.showerror(t("error_title"), self._friendly(err), parent=self)

    def _when_text(self, dt: datetime) -> str:
        return f"{i18n.weekday_short(dt.weekday())} {dt:%H:%M}"

    # ---- map ----

    def _marker(self, lat, lon, text, color, big=False, size=None):
        return self.map.set_marker(lat, lon, text=text, text_color=color,
                                   marker_color_outside=color,
                                   marker_color_circle=color if big else "white",
                                   font=("Segoe UI", size or (11 if big else 9), "bold"))

    def _clear_map(self):
        self.map.delete_all_marker()
        self.map.delete_all_path()
        self._place_markers.clear()

    def _fit(self, points):
        if not points:
            return
        if len(points) == 1:
            self.map.set_position(*points[0])
            self.map.set_zoom(16)
            return
        lats, lons = [p[0] for p in points], [p[1] for p in points]
        pad_lat = max(0.0008, (max(lats) - min(lats)) * 0.12)   # 0.0008 deg is about 90 m
        pad_lon = max(0.0008, (max(lons) - min(lons)) * 0.12)
        self.map.fit_bounding_box((max(lats) + 2 * pad_lat, min(lons) - pad_lon),   # pins point down: room on top
                                  (min(lats) - pad_lat, max(lons) + pad_lon))

    def on_place_changed(self, field: PlaceField, fit: bool = True):
        old = self._place_markers.pop(field, None)
        if old is not None:
            old.delete()
        if field.place is not None:
            tag = {self.from_field: "A", self.to_field: "B"}.get(field)
            text = tag or f"●  {field.place.label}"     # trip ends get short labels so they don't overlap
            self._place_markers[field] = self._marker(field.place.lat, field.place.lon, text,
                                                      COLOR["place"], size=12 if tag else None)
        if fit:
            shown = [f.place for f in self._place_markers if f.place is not None]
            self._fit([(p.lat, p.lon) for p in shown])

    # ---- fresh data before acting ----

    def with_fresh_data(self, then):
        age = self.data.live_age_min()
        if age is None:
            if messagebox.askyesno(t("no_data_title"), t("no_data_q"), parent=self):
                self.update_snapshot(then)
            return
        if age > STALE_AFTER_MIN and messagebox.askyesno(
                t("stale_title"), t("stale_q", minutes=int(age)), parent=self):
            self.update_snapshot(then)
            return
        then()

    def update_snapshot(self, then=None):
        self.set_busy(True, t("updating"))

        def done(_):
            self.set_busy(False)
            self.refresh_status()
            self.worker.run(self.data.history, lambda _: self.refresh_status())
            if then:
                then()
        self.worker.run(self.data.update, done)

    def on_update(self):
        if not self.busy:
            self.update_snapshot()

    def ensure_places(self, fields, then):
        """Search any field whose text hasn't been looked up yet (one at a time), then call
        then(ok) where ok means every field has a place."""
        pending = [f for f in fields if f.text() and not f.resolved()]

        def step(_ok=True):
            if pending:
                pending.pop(0).search(then=step)
            else:
                then(all(f.resolved() for f in fields))
        step()

    # ---- trip ----

    def _when_changed(self, value):
        state = "disabled" if value == t("now") else "normal"
        self.day_menu.configure(state=state)
        self.time_entry.configure(state=state)
        if state == "normal" and not self.time_entry.get():
            soon = planner.now_barcelona() + timedelta(minutes=30)
            self.time_entry.insert(0, f"{soon.hour:02d}:{30 if soon.minute >= 30 else 0:02d}")

    def _trip_params(self):
        params = {"bike_type": code_of(BIKE_KEYS, self.bike_seg.get(), "either"),
                  "radius": int(self.walk_slider.get())}
        mode = code_of(WHEN_KEYS, self.when_seg.get(), "now")
        if mode == "now":
            return params
        hm = parse_hhmm(self.time_entry.get())
        if hm is None:
            self.write_results([(t("bad_time"), "warn")])
            return None
        when = planner.now_barcelona().replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        if self.day_menu.get() == t("tomorrow"):
            when += timedelta(days=1)
        params["arrive_by" if mode == "arrive_by" else "depart"] = when
        return params

    def on_plan(self):
        if self.busy:
            return

        def after_places(ok):
            if not ok:
                self.write_results([(t("need_places"), "warn")])
                return
            self.with_fresh_data(self._run_plan)
        self.ensure_places([self.from_field, self.to_field], after_places)

    def _run_plan(self):
        params = self._trip_params()
        if params is None:
            return
        a, b = self.from_field.place, self.to_field.place
        self.set_busy(True, t("planning"))

        def job():
            stations = self.data.stations()
            hist, n = self.data.history()
            return planner.plan_trip((a.label, a.lat, a.lon), (b.label, b.lat, b.lon),
                                     stations, hist, **params), n
        self.worker.run(job, self._show_plan)

    def _now_line(self, row, role, bike_type):
        if role == "destination":
            return t("now_docks", count=row["empty_slots"])
        if bike_type == "ebike":
            return t("now_bikes", count=row["ebikes"], what=t("what_ebikes"))
        if bike_type == "mechanical":
            return t("now_bikes", count=row["normal_bikes"], what=t("what_mech"))
        return t("now_bikes", count=row["free_bikes"],
                 what=t("what_bikes", mech=row["normal_bikes"], e=row["ebikes"]))

    def _show_plan(self, result):
        self._last_result = ("plan", result)
        plan, n_snap = result
        self.set_busy(False)
        self.refresh_status()
        a, b = plan.origin, plan.destination          # (label, lat, lon)

        # map
        self._clear_map()
        points = [(a[1], a[2]), (b[1], b[2])]
        self._place_markers[self.from_field] = self._marker(a[1], a[2], "A", COLOR["place"], size=12)
        self._place_markers[self.to_field] = self._marker(b[1], b[2], "B", COLOR["place"], size=12)
        ends = (("origin", plan.picks, COLOR["pick"], t("pick_up_short")),
                ("destination", plan.drops, COLOR["drop"], t("drop_off_short")))
        for role, ranked, color, label in ends:
            for i, r in reversed(list(enumerate(ranked[:MAP_CANDIDATES]))):   # best drawn last (on top)
                if i == 0:
                    self._marker(r["lat"], r["lon"], f"{label}: {r['station']}", color, big=True)
                else:
                    self._marker(r["lat"], r["lon"], f"{r['reliability']:.0%}", reliability_color(r))
                points.append((r["lat"], r["lon"]))
        if plan.picks and plan.drops:
            p, d = plan.picks[0], plan.drops[0]
            self.map.set_path([(a[1], a[2]), (p["lat"], p["lon"])], color=COLOR["walk"], width=2)
            self.map.set_path([(p["lat"], p["lon"]), (d["lat"], d["lon"])], color=COLOR["ride"], width=4)
            self.map.set_path([(d["lat"], d["lon"]), (b[1], b[2])], color=COLOR["walk"], width=2)
        self._fit(points)

        # text
        width = max(len(t("pick_up")), len(t("drop_off"))) + 1
        indent = " " * (width + 1)
        parts = [(t("trip_header", a=a[0], b=b[0]) + "\n", "h1"),
                 (t("trip_sub", bike=t(f"trip_bike_{plan.bike_type}"), depart=self._when_text(plan.depart),
                    ride=round(plan.ride_min), arrive=f"{plan.arrive:%H:%M}") + "\n", None),
                 ((t("history_basis", n=n_snap) if n_snap else t("no_history_note")) + "\n\n", "muted")]
        for role, ranked, radius in (("origin", plan.picks, plan.pick_radius),
                                     ("destination", plan.drops, plan.drop_radius)):
            title, tag = (t("pick_up"), "pick") if role == "origin" else (t("drop_off"), "drop")
            if not ranked:
                parts.append((f"{title}  {t('no_station', r=radius)}\n\n", "warn"))
                continue
            best = ranked[0]
            key = "walk_reliab_pick" if role == "origin" else "walk_reliab_drop"
            parts.append((f"{title:<{width}} {best['station']}\n", tag))
            parts.append((indent + t(key, walk=best["walk_m"], p=f"{best['reliability']:.0%}",
                                     basis=basis_text(best["basis"]), n=best["samples"]) + "\n", None))
            parts.append((indent + self._now_line(best, role, plan.bike_type) + "\n", "muted"))
            closer = [r for r in ranked if r is not best and r["walk_m"] < best["walk_m"]][:3]
            if closer:
                items = ", ".join(f"{r['station']} ({r['walk_m']} m, {r['reliability']:.0%})" for r in closer)
                parts.append((indent + t("skipped", items=items) + "\n", "muted"))
            if radius > plan.requested_radius:
                parts.append((indent + t("widened", r0=plan.requested_radius, r=radius) + "\n", "muted"))
            if not best["live_ok"]:
                parts.append((indent + t("warn_now") + "\n", "warn"))
            parts.append(("\n", None))

        others = [(t("pick_up_short"), r, "origin") for r in plan.picks[1:1 + TEXT_OPTIONS]] + \
                 [(t("drop_off_short"), r, "destination") for r in plan.drops[1:1 + TEXT_OPTIONS]]
        if others:
            lw = max(len(t("pick_up_short")), len(t("drop_off_short")))
            ww = max(len(t("col_walk")), 6)
            rw = max(len(t("col_reliab")), 4)
            parts.append((t("other_options") + "\n", "h1"))
            parts.append((f"  {'':<{lw}} {t('col_station'):<40} {t('col_walk'):>{ww}}  "
                          f"{t('col_reliab'):>{rw}}  {t('col_now')}\n", "muted"))
            for label, r, role in others:
                now = r["empty_slots"] if role == "destination" else (
                    r["ebikes"] if plan.bike_type == "ebike" else
                    r["normal_bikes"] if plan.bike_type == "mechanical" else r["free_bikes"])
                parts.append((f"  {label:<{lw}} {r['station'][:40]:<40} {str(r['walk_m']) + ' m':>{ww}}  "
                              f"{r['reliability']:>{rw}.0%}  {now}\n", None))
        self.write_results(parts)
        self.export_rows = plan.picks[:EXPORT_CANDIDATES] + plan.drops[:EXPORT_CANDIDATES]
        self.export_kind = "trip"
        self.save_btn.configure(state="normal" if self.export_rows else "disabled")

    # ---- nearby ----

    def on_nearby(self):
        if self.busy:
            return

        def after_place(ok):
            if not ok:
                self.write_results([(t("need_place"), "warn")])
                return
            self.with_fresh_data(self._run_nearby)
        self.ensure_places([self.near_field], after_place)

    def _run_nearby(self):
        p = self.near_field.place
        need = code_of(NEED_KEYS, self.need_seg.get(), "bikes")
        radius = int(self.radius_slider.get())
        self.set_busy(True, t("finding"))
        self.worker.run(lambda: (planner.nearby((p.label, p.lat, p.lon), self.data.stations(),
                                                need=need, radius=radius), need, radius, (p.label, p.lat, p.lon)),
                        self._show_nearby)

    def _show_nearby(self, result):
        self._last_result = ("nearby", result)
        (rows, inside), need, radius, place = result
        self.set_busy(False)
        self.refresh_status()
        label, lat, lon = place
        self._clear_map()
        self._place_markers[self.near_field] = self._marker(lat, lon, f"●  {label}", COLOR["place"], big=True)
        points = [(lat, lon)]
        for r in reversed(rows):
            self._marker(r["lat"], r["lon"], str(r["count"]) if r["online"] else "×",
                         STATUS_COLOR[r["status"]], size=12)
            points.append((r["lat"], r["lon"]))
        self._fit(points)

        age = self.data.live_age_min() or 0
        parts = [(t("nearby_header", place=label) + "\n", "h1"),
                 (t("nearby_sub", need=t(NEED_KEYS[need]), r=radius, age=self._age_text(age)) + "\n\n", "muted")]
        if not inside:
            parts.append((t("nearby_fallback", r=radius) + "\n", "warn"))
        ww = max(len(t("col_walk")), 6)
        bw = max(len(t("col_bikes")), 12)
        dw = max(len(t("col_docks")), 5)
        parts.append((f"{t('col_station'):<42} {t('col_walk'):>{ww}}  {t('col_bikes'):>{bw}}  "
                      f"{t('col_docks'):>{dw}}\n", "muted"))
        for r in rows:
            bikes = f"{r['free_bikes']} ({r['normal_bikes']}/{r['ebikes']})"
            tag = {"ok": "pick", "low": None, "none": "warn", "offline": "muted"}[r["status"]]
            parts.append((f"{r['station'][:42]:<42} {str(r['walk_m']) + ' m':>{ww}}  {bikes:>{bw}}  "
                          f"{r['empty_slots']:>{dw}}  {t('status_' + r['status'])}\n", tag))
        self.write_results(parts)
        self.export_rows, self.export_kind = rows, "nearby"
        self.save_btn.configure(state="normal" if rows else "disabled")

    # ---- export ----

    def on_save_csv(self):
        if not self.export_rows:
            messagebox.showinfo(t("app_title"), t("nothing_to_save"), parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=f"bicing_{self.export_kind}_{datetime.now():%Y%m%d_%H%M}.csv")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:   # BOM so Excel shows accents
            w = csv.DictWriter(f, fieldnames=list(self.export_rows[0]))
            w.writeheader()
            w.writerows(self.export_rows)
        self.busy_label.configure(text=t("saved_to", n=len(self.export_rows), path=path))

    # ---- Jev ----

    def on_ask(self):
        if self.busy:
            return
        key, _ = app_config.api_key()
        if not key:
            self.open_settings()
            return
        text = self.ask_entry.get().strip()
        if not text:
            return
        self.set_busy(True, t("asking_jev"))

        def job():
            import jev_assistant   # imported lazily: keeps start-up fast
            return jev_assistant.parse(text, key, now=planner.now_barcelona())
        self.worker.run(job, self._apply_fill)

    def _apply_fill(self, fill):
        self.set_busy(False)
        if fill.intent == "other":
            self.jev_status.configure(text=t("jev_other"))
            return
        if fill.intent == "find_nearby":
            self.select_tab(t("tab_nearby"))
            self.near_field.set_text(fill.nearby_place or "")
            need = "docks" if fill.nearby_need == "docks" else (
                fill.bike_type if fill.bike_type in ("ebike", "mechanical") else "bikes")
            self.need_seg.set(t(NEED_KEYS[need]))
            self._set_slider(self.radius_slider, self.radius_value, fill.walk_m)
            msg = t("jev_nearby", need=t(NEED_KEYS[need]).lower(), place=fill.nearby_place or "?",
                    walk=int(self.radius_slider.get()))
            fields = [self.near_field]
        else:
            self.select_tab(t("tab_trip"))
            self.from_field.set_text(fill.origin or "")
            self.to_field.set_text(fill.destination or "")
            bike = fill.bike_type
            uphill_hint = bike == "either" and fill.uphill > 0.5
            if uphill_hint:
                bike = "ebike"
            self.bike_seg.set(t(BIKE_KEYS[bike]))
            mode = t(WHEN_KEYS[fill.when])
            self.when_seg.set(mode)
            self._when_changed(mode)
            day = t("tomorrow") if fill.day == "tomorrow" else t("today")
            if fill.time_hm:
                self.day_menu.set(day)
                self.time_entry.delete(0, "end")
                self.time_entry.insert(0, f"{fill.time_hm[0]:02d}:{fill.time_hm[1]:02d}")
            self._set_slider(self.walk_slider, self.walk_value, fill.walk_m)
            when_text = (t("jev_when_now") if fill.when == "now" else
                         t("jev_when_leave" if fill.when == "depart_at" else "jev_when_arrive",
                           day=day.lower(), time=self.time_entry.get()))
            msg = t("jev_trip", from_=fill.origin or "?", to=fill.destination or "?", when=when_text,
                    bike=t(f"trip_bike_{bike}").lower(), walk=int(self.walk_slider.get()))
            if not fill.origin:
                msg += t("jev_missing_from")
            if not fill.destination:
                msg += t("jev_missing_to")
            if uphill_hint:
                msg += t("jev_uphill")
            fields = [self.from_field, self.to_field]
        if fill.unsure:
            msg += t("jev_unsure", fields=", ".join(t(f"field_{f}") for f in fill.unsure))
        self.jev_status.configure(text=msg)
        self.ensure_places([f for f in fields if f.text()], lambda ok: None)

    # ---- settings ----

    def open_settings(self):
        win = ctk.CTkToplevel(self)
        win.title(t("settings_title"))
        win.geometry("560x250")
        win.resizable(False, False)
        win.transient(self)
        if ICON_FILE.exists():
            win.after(250, lambda: win.iconbitmap(str(ICON_FILE)))
        key, source = app_config.api_key()
        ctk.CTkLabel(win, text=t("key_label"), font=self.bold).pack(anchor="w", padx=18, pady=(18, 4))
        info = {"env": t("key_from_env"), "config": t("key_from_config"), "missing": t("key_missing")}[source]
        ctk.CTkLabel(win, text=info, wraplength=520, justify="left", anchor="w").pack(anchor="w", padx=18)
        entry = ctk.CTkEntry(win, show="•", width=520)
        entry.pack(padx=18, pady=12)
        if source == "config":
            entry.insert(0, key)
        buttons = ctk.CTkFrame(win, fg_color="transparent")
        buttons.pack(fill="x", padx=18, pady=(4, 16))

        def save():
            app_config.set_api_key(entry.get())
            self.refresh_key_state()
            win.destroy()

        def remove():
            app_config.set_api_key("")
            self.refresh_key_state()
            win.destroy()
        ctk.CTkButton(buttons, text=t("save"), width=110, command=save).pack(side="left")
        ctk.CTkButton(buttons, text=t("remove_key"), width=170, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=remove).pack(side="left", padx=8)
        ctk.CTkButton(buttons, text=t("close"), width=90, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=win.destroy).pack(side="right")
        win.after(150, lambda: (win.grab_set(), entry.focus_set()))


def selftest() -> int:
    """`BicingPlanner.exe --selftest`: checks the bundled modules, data and online services without
    opening the window, and writes the report to selftest.txt in the data folder."""
    report = []

    def check(name, fn):
        try:
            report.append(f"OK    {name}: {fn()}")
        except Exception as e:
            report.append(f"FAIL  {name}: {type(e).__name__}: {e}")

    def jev():
        import jev_assistant
        key, source = app_config.api_key()
        if not key:
            return "skipped (no API key)"
        fill = jev_assistant.parse("bici de la plaça de Catalunya a Sants", key, now=planner.now_barcelona())
        return f"key from {source}; {fill.intent}: {fill.origin} -> {fill.destination}"

    check("data folder", lambda: str(app_paths.DATA_DIR))
    check("stations (bundled/saved)", lambda: len(planner.load_live(str(LIVE_CSV))))
    check("history snapshots", lambda: planner.read_history(str(HISTORY_CSV))[1])
    check("live data download", lambda: f"{len(citybikes.fetch_stations())} stations")
    check("address search", lambda: geocode.search("Estació de Sants")[0].label)
    check("trip plan", lambda: planner.plan_trip(
        ("A", 41.3870, 2.1700), ("B", 41.3792, 2.1400), planner.load_live(str(LIVE_CSV)),
        planner.read_history(str(HISTORY_CSV))[0]).picks[0]["station"])
    check("Jev", jev)
    out = app_paths.DATA_DIR / "selftest.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(report) + "\n", encoding="utf-8")
    return 0 if all(line.startswith("OK") for line in report) else 1


def main():
    if app_paths.FROZEN:
        app_paths.seed_data()
    i18n.set_language(app_config.language() or i18n.detect_language())
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("blue")
    App().mainloop()


if __name__ == "__main__":
    main()
