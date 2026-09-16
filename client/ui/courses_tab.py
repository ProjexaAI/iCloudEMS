"""Course-history tab.

Three panes, left to right:
    Subjects -> Students -> this student's lecture-by-lecture attendance
                            in the selected subject.

Design:
- One "Load" click fans out to a server-side job that fetches the
  timetable + every roster in range. Progress shown inline.
- Staged / Aggregated editing: Clicking attendance cells stages the changes
  locally in real time (instant visual feedback + updated percentages & class
  presence counts) without firing immediate API writes.
- A "Submit Changes" button sends all modified slots aggregately in a batch.
- When the batch finishes, the frontend reconciles and refreshes with verified
  server data for those classes.
"""
import time
import csv
import tkinter as tk
from datetime import date, timedelta, datetime
from tkinter import ttk, messagebox

from .api import ServerError


class CoursesTab:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        self.courses = []
        self.course_by_key = {}
        self.selected_course_key = None
        self.selected_student_admno = None
        self.poll_active = False
        self.poll_after_id = None
        self.dirty_slots = set()  # set of (course_key, slot_key)
        self.failed_slots = []
        self._build()

    # ---------- build ----------

    def _build(self):
        top = ttk.Frame(self.parent, padding=(16, 12, 16, 0))
        top.pack(fill="x")
        ttk.Label(top, text="From").pack(side="left")
        default_from = (date.today() - timedelta(days=30)).isoformat()
        self.from_var = tk.StringVar(value=default_from)
        ttk.Entry(top, textvariable=self.from_var, width=12).pack(side="left", padx=6)
        ttk.Label(top, text="To").pack(side="left")
        self.to_var = tk.StringVar(value=date.today().isoformat())
        ttk.Entry(top, textvariable=self.to_var, width=12).pack(side="left", padx=6)
        self.load_btn = ttk.Button(top, text="Load", style="Primary.TButton",
                                   command=self.on_load)
        self.load_btn.pack(side="left", padx=6)
        self.sync_btn = ttk.Button(top, text="Sync now", style="Ghost.TButton",
                                   command=self.on_sync)
        self.sync_btn.pack(side="left", padx=2)
        self.sync_subject_btn = ttk.Button(
            top, text="Sync subject", style="Ghost.TButton",
            command=self.on_sync_subject,
        )
        self.sync_subject_btn.pack(side="left", padx=2)
        self.sync_day_btn = ttk.Button(
            top, text="Sync day", style="Ghost.TButton",
            command=self.on_sync_day,
        )
        self.sync_day_btn.pack(side="left", padx=2)
        self.retry_btn = ttk.Button(
            top, text="Retry failed", style="Ghost.TButton",
            state="disabled", command=self.on_retry_failed,
        )
        self.retry_btn.pack(side="left", padx=2)
        self.progress_label = ttk.Label(top, text="", style="Muted.TLabel")
        self.progress_label.pack(side="left", padx=12)

        body = ttk.Frame(self.parent, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1, minsize=300)
        body.columnconfigure(1, weight=1, minsize=260)
        body.columnconfigure(2, weight=2)
        body.rowconfigure(0, weight=1)

        # --- left: subjects with aggregate stats ---
        left = ttk.Frame(body, style="Card.TFrame", padding=12)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ttk.Label(left, text="Subjects", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(left,
                  text="Present / absent counts across all students × all taken slots.",
                  style="Muted.TLabel", font=("Segoe UI", 8, "italic"),
                  wraplength=260).pack(anchor="w", pady=(2, 6))

        cols = ("subject", "present", "absent", "pct")
        self.course_tree = ttk.Treeview(left, columns=cols,
                                        show="headings", height=20)
        self.course_tree.heading("subject", text="Subject · Div")
        self.course_tree.heading("present", text="Pres")
        self.course_tree.heading("absent", text="Abs")
        self.course_tree.heading("pct", text="%")
        self.course_tree.column("subject", width=170, anchor="w")
        self.course_tree.column("present", width=50, anchor="center")
        self.course_tree.column("absent", width=50, anchor="center")
        self.course_tree.column("pct", width=55, anchor="center")
        self.course_tree.pack(fill="both", expand=True, pady=(4, 0))
        self.course_tree.bind("<<TreeviewSelect>>", self.on_course_selected)

        # --- middle: students in selected subject ---
        mid = ttk.Frame(body, style="Card.TFrame", padding=12)
        mid.grid(row=0, column=1, sticky="nsew", padx=(0, 8))
        ttk.Label(mid, text="Students", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(mid, text="Click a student to see their lectures.",
                  style="Muted.TLabel", font=("Segoe UI", 8, "italic")
                  ).pack(anchor="w", pady=(2, 6))
        filter_row = ttk.Frame(mid, style="Card.TFrame")
        filter_row.pack(fill="x", pady=(0, 4))
        ttk.Label(filter_row, text="Search").pack(side="left")
        self.student_filter_var = tk.StringVar()
        self.student_filter_var.trace_add("write", lambda *_: self._refresh_student_tree())
        ttk.Entry(filter_row, textvariable=self.student_filter_var, width=16).pack(side="left", padx=5)
        self.absent_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            filter_row, text="Below 75%", variable=self.absent_only_var,
            command=self._refresh_student_tree,
        ).pack(side="left")

        cols = ("rollno", "name", "pct")
        self.student_tree = ttk.Treeview(mid, columns=cols,
                                         show="headings", height=20)
        self.student_tree.heading("rollno", text="Roll")
        self.student_tree.heading("name", text="Name")
        self.student_tree.heading("pct", text="%")
        self.student_tree.column("rollno", width=80, anchor="w")
        self.student_tree.column("name", width=140, anchor="w")
        self.student_tree.column("pct", width=55, anchor="center")
        self.student_tree.pack(fill="both", expand=True, pady=(4, 0))
        self.student_tree.bind("<<TreeviewSelect>>", self.on_student_selected)

        # --- right: this student's attendance & staged actions ---
        right = ttk.Frame(body, style="Card.TFrame", padding=12)
        right.grid(row=0, column=2, sticky="nsew")
        self.att_header = ttk.Label(right, text="Select a student",
                                    style="CardTitle.TLabel")
        self.att_header.pack(anchor="w")
        self.att_subheader = ttk.Label(
            right,
            text="Click a Status cell to stage edits. Click 'Submit Changes' to save.",
            style="Muted.TLabel", font=("Segoe UI", 8, "italic"),
        )
        self.att_subheader.pack(anchor="w", pady=(2, 6))

        cols = ("date", "time", "status", "class_status")
        self.att_tree = ttk.Treeview(right, columns=cols,
                                     show="headings", height=17)
        self.att_tree.heading("date", text="Date")
        self.att_tree.heading("time", text="Time")
        self.att_tree.heading("status", text="Student Status")
        self.att_tree.heading("class_status", text="Class Attendance")
        self.att_tree.column("date", width=95, anchor="w")
        self.att_tree.column("time", width=95, anchor="w")
        self.att_tree.column("status", width=115, anchor="center")
        self.att_tree.column("class_status", width=155, anchor="center")
        self.att_tree.pack(fill="both", expand=True, pady=(0, 6))
        self.att_tree.bind("<Button-1>", self.on_att_click)

        # Tags for styled feedback
        self.att_tree.tag_configure("staged", foreground="#2563eb")
        self.att_tree.tag_configure("not_enrolled", foreground="#9ca3af")

        # Action bar: Submit, Discard, Pending summary
        actions_bar = ttk.Frame(right, style="Card.TFrame")
        actions_bar.pack(fill="x", pady=(2, 6))

        self.submit_btn = ttk.Button(
            actions_bar, text="Submit Changes", style="Primary.TButton",
            state="disabled", command=self.on_submit_changes
        )
        self.submit_btn.pack(side="left")

        self.discard_btn = ttk.Button(
            actions_bar, text="Discard Changes", style="Ghost.TButton",
            state="disabled", command=self.on_discard_changes
        )
        self.discard_btn.pack(side="left", padx=8)

        self.pending_label = ttk.Label(actions_bar, text="", style="Muted.TLabel")
        self.pending_label.pack(side="left", padx=8)

        self.att_summary = ttk.Label(right, text="",
                                     style="CardTitle.TLabel",
                                     font=("Segoe UI", 11, "bold"))
        self.att_summary.pack(anchor="w")
        self.analytics_label = ttk.Label(right, text="", style="Muted.TLabel")
        self.analytics_label.pack(anchor="w", pady=(4, 0))
        ttk.Button(
            right, text="Export subject CSV", style="Ghost.TButton",
            command=self.export_subject_csv,
        ).pack(anchor="w", pady=(4, 0))

    # ---------- state helpers ----------

    def reset(self):
        self.poll_active = False
        if self.poll_after_id:
            try:
                self.parent.after_cancel(self.poll_after_id)
            except Exception:
                pass
            self.poll_after_id = None
        self.courses = []
        self.course_by_key = {}
        self.failed_slots = []
        self.retry_btn.config(state="disabled")
        self.selected_course_key = None
        self.selected_student_admno = None
        self.dirty_slots.clear()
        for tree in (self.course_tree, self.student_tree, self.att_tree):
            tree.delete(*tree.get_children())
        self.progress_label.config(text="")
        self.att_header.config(text="Select a student")
        self.att_summary.config(text="")
        self.analytics_label.config(text="")
        self._update_action_buttons()

    def _update_course_analytics(self):
        course = self.course_by_key.get(self.selected_course_key)
        if not course:
            self.analytics_label.config(text="")
            return
        present, absent = self._course_totals(course)
        total = present + absent
        average = round(100 * present / total, 1) if total else 0
        low_count = 0
        for student in course.get("students", []):
            row = course.get("matrix", {}).get(student["admno"], {})
            count = len(row)
            attended = sum(1 for value in row.values() if value)
            if count and attended / count < 0.75:
                low_count += 1
        self.analytics_label.config(
            text=f"Class average: {average}% · {low_count} students below 75%"
        )

    def export_subject_csv(self):
        course = self.course_by_key.get(self.selected_course_key)
        if not course:
            messagebox.showinfo("Export", "Select a subject first.")
            return
        path = f"attendance_{course.get('subjectId') or 'subject'}.csv"
        try:
            with open(path, "w", newline="", encoding="utf-8") as output:
                writer = csv.writer(output)
                writer.writerow(["Admission number", "Roll number", "Name", "Present", "Total", "Percentage"])
                for student in course.get("students", []):
                    row = course.get("matrix", {}).get(student["admno"], {})
                    present = sum(1 for value in row.values() if value)
                    total = len(row)
                    pct = round(100 * present / total, 1) if total else 0
                    writer.writerow([
                        student["admno"], student.get("rollno", ""),
                        student.get("name", ""), present, total, pct,
                    ])
            self.app.set_status(f"Exported {path}")
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc))

    def _slot_stats(self, course, sk):
        """Return (present_count, absent_count, total_count, is_taken) for a slot."""
        matrix = course.get("matrix", {})
        p = sum(1 for row in matrix.values() if sk in row and row[sk])
        t = sum(1 for row in matrix.values() if sk in row)
        a = t - p
        slot = next((s for s in course.get("slots", []) if s["slot_key"] == sk), None)
        is_taken = bool(slot.get("taken")) and bool(slot.get("update_id"))
        return p, a, t, is_taken

    def _is_slot_dirty(self, course_key, sk):
        c = self.course_by_key.get(course_key)
        if not c:
            return False
        matrix = c.get("matrix", {})
        orig = c.get("orig_matrix", {})
        for adm, row in matrix.items():
            if sk in row:
                if row[sk] != orig.get(adm, {}).get(sk):
                    return True
        return False

    def _count_total_staged_changes(self):
        total = 0
        for ck, sk in self.dirty_slots:
            c = self.course_by_key.get(ck)
            if not c:
                continue
            matrix = c.get("matrix", {})
            orig = c.get("orig_matrix", {})
            for adm, row in matrix.items():
                if sk in row and row[sk] != orig.get(adm, {}).get(sk):
                    total += 1
        return total

    def _update_action_buttons(self):
        total_changes = self._count_total_staged_changes()
        if total_changes > 0:
            self.submit_btn.config(state="normal", text=f"Submit Changes ({total_changes})")
            self.discard_btn.config(state="normal")
            self.pending_label.config(
                text=f"{total_changes} change(s) staged across {len(self.dirty_slots)} lecture(s)"
            )
        else:
            self.submit_btn.config(state="disabled", text="Submit Changes")
            self.discard_btn.config(state="disabled")
            self.pending_label.config(text="")

    # ---------- load + polling ----------

    def on_load(self, force=False, subject_id=None, sync_day=None, slot_keys=None):
        if self.dirty_slots:
            confirm = messagebox.askyesno(
                "Unsaved Changes",
                "You have unsubmitted staged attendance changes. Discard and reload?"
            )
            if not confirm:
                return

        df = self.from_var.get().strip()
        dt = self.to_var.get().strip()
        try:
            datetime.strptime(df, "%Y-%m-%d")
            datetime.strptime(dt, "%Y-%m-%d")
        except ValueError:
            messagebox.showwarning("Invalid dates",
                                   "Use YYYY-MM-DD for both dates.")
            return

        self.reset()
        self.load_btn.config(state="disabled", text="Loading…")
        self.progress_label.config(text="Starting…")
        self.app.set_status("Loading course history…")

        def do():
            return self.app.api.load_courses(
                df, dt, force=force, subject_id=subject_id,
                sync_day=sync_day, slot_keys=slot_keys,
            )

        def ok(data):
            self._start_polling(data["job_id"])

        def err(e):
            self.load_btn.config(state="normal", text="Load")
            self.progress_label.config(text="")
            messagebox.showerror("Load failed",
                                 getattr(e, "message", None) or str(e))

        self.app.run_async(do, ok, err)

    def on_sync_status(self):
        """Show the mirror status, then let the user choose the next sync range."""
        def do():
            return self.app.api.sync_status()

        def ok(data):
            status = data.get("status", "never_synced")
            done = data.get("slots_done", 0)
            total = data.get("slots_total", 0)
            if status == "running":
                message = f"Sync in progress: {done}/{total} slots"
            elif status == "done":
                message = f"Last sync complete: {done}/{total} slots"
            elif status == "error":
                message = f"Last sync failed: {data.get('error') or 'unknown error'}"
            else:
                message = "No sync has completed yet."
            self.progress_label.config(text=message)
            self.app.set_status(message)
            if message.startswith("No sync"):
                self.on_load()

        def err(e):
            messagebox.showerror("Sync status failed",
                                 getattr(e, "message", None) or str(e))

        self.app.run_async(do, ok, err)

    def on_sync(self):
        """Run a fresh upstream sync for the selected date range."""
        self.on_load(force=True)

    def on_sync_subject(self):
        """Refresh only the currently selected subject."""
        if not self.selected_course_key:
            messagebox.showinfo("Select a subject", "Select a subject before syncing it.")
            return
        course = self.course_by_key.get(self.selected_course_key)
        if not course or course.get("subjectId") is None:
            messagebox.showerror("Sync subject", "The selected subject has no subject ID.")
            return
        self.on_load(force=True, subject_id=str(course["subjectId"]))

    def on_sync_day(self):
        """Refresh only the selected calendar day in the current range."""
        day = self.to_var.get().strip()
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            messagebox.showwarning("Invalid date", "Use YYYY-MM-DD for the sync day.")
            return
        self.on_load(force=True, sync_day=day)

    def on_retry_failed(self):
        if not self.failed_slots:
            return
        slot_keys = [slot["slot_key"] for slot in self.failed_slots]
        self.on_load(force=True, slot_keys=slot_keys)

    def _start_polling(self, job_id):
        self.poll_active = True
        self._poll_once(job_id)

    def _poll_once(self, job_id):
        if not self.poll_active:
            return

        def do():
            return self.app.api.job_status(job_id)

        def ok(data):
            if not self.poll_active:
                return
            status = data.get("status")
            progress = data.get("progress") or {}
            if status == "running":
                phase = progress.get("phase", "")
                done = progress.get("done", 0)
                total = progress.get("total", 0)
                if total:
                    self.progress_label.config(text=f"{phase}: {done}/{total}")
                else:
                    self.progress_label.config(text=phase)
                self.poll_after_id = self.parent.after(
                    800, lambda: self._poll_once(job_id))
            elif status == "done":
                self.load_btn.config(state="normal", text="Load")
                self.progress_label.config(text="Ready.")
                self.app.set_status("Course history loaded.")
                self._apply_result(data.get("result") or {})
            else:
                self.load_btn.config(state="normal", text="Load")
                self.progress_label.config(text="")
                messagebox.showerror("Load failed",
                                     data.get("error") or "unknown error")

        def err(e):
            if not self.poll_active:
                return
            self.load_btn.config(state="normal", text="Load")
            self.progress_label.config(text="")
            messagebox.showerror("Poll failed",
                                 getattr(e, "message", None) or str(e))

        self.app.run_async(do, ok, err)

    def _apply_result(self, result):
        self.failed_slots = result.get("failed_slots", [])
        self.retry_btn.config(
            state="normal" if self.failed_slots else "disabled"
        )
        cached = result.get("cached_slots", 0)
        fetched = result.get("fetched_slots", 0)
        failed = len(self.failed_slots)
        if failed:
            self.progress_label.config(
                text=f"Cached {cached} · fetched {fetched} · failed {failed}"
            )
        else:
            self.progress_label.config(text=f"Cached {cached} · fetched {fetched}")
        self.courses = result.get("courses", [])
        self.courses.sort(key=lambda c: (c.get("subject") or "",
                                         c.get("division") or ""))
        self.course_by_key = {c["key"]: c for c in self.courses}
        self.dirty_slots.clear()

        for c in self.courses:
            # Baseline copy of matrix for tracking staged changes
            c["orig_matrix"] = {adm: dict(row) for adm, row in c.get("matrix", {}).items()}
            p, a = self._course_totals(c)
            pct = round(100 * p / (p + a)) if (p + a) else 0
            label = c["subject"]
            if c.get("division"):
                label += f" · {c['division']}"
            if c.get("batch"):
                label += f" / {c['batch']}"
            self.course_tree.insert("", "end", iid=c["key"],
                                    values=(label, p, a, f"{pct}%"))
        self._update_action_buttons()

    def _course_totals(self, c):
        """(present_events, absent_events) across all students × all slots."""
        tp = ta = 0
        for row in c.get("matrix", {}).values():
            for v in row.values():
                if v:
                    tp += 1
                else:
                    ta += 1
        return tp, ta

    # ---------- selection handlers ----------

    def on_course_selected(self, _event=None):
        sel = self.course_tree.selection()
        if not sel:
            return
        key = sel[0]
        self.selected_course_key = key
        self.selected_student_admno = None
        c = self.course_by_key.get(key)
        if not c:
            return

        self.student_filter_var.set("")
        self.absent_only_var.set(False)
        self._refresh_student_tree()

        self.att_tree.delete(*self.att_tree.get_children())
        self.att_header.config(text="Select a student")
        self.att_summary.config(text="")
        self._update_course_analytics()

    def _refresh_student_tree(self):
        c = self.course_by_key.get(self.selected_course_key)
        if not c:
            return
        query = self.student_filter_var.get().strip().lower()
        below_threshold = self.absent_only_var.get()
        self.student_tree.delete(*self.student_tree.get_children())
        for student in c.get("students", []):
            row = c["matrix"].get(student["admno"], {})
            present = sum(1 for value in row.values() if value)
            total = len(row)
            pct = round(100 * present / total) if total else 0
            haystack = f"{student.get('rollno', '')} {student.get('name', '')} {student['admno']}".lower()
            if query and query not in haystack:
                continue
            if below_threshold and pct >= 75:
                continue
            orig_row = c.get("orig_matrix", {}).get(student["admno"], {})
            is_dirty = any(row.get(sk) != orig_row.get(sk) for sk in row)
            pct_str = f"{pct}% *" if is_dirty else f"{pct}%"
            self.student_tree.insert(
                "", "end", iid=student["admno"],
                values=(student["rollno"], student["name"], pct_str),
            )

    def on_student_selected(self, _event=None):
        sel = self.student_tree.selection()
        if not sel:
            return
        admno = sel[0]
        self.selected_student_admno = admno
        c = self.course_by_key.get(self.selected_course_key)
        if not c:
            return

        row = c["matrix"].get(admno, {})
        orig_row = c.get("orig_matrix", {}).get(admno, {})
        self.att_tree.delete(*self.att_tree.get_children())
        for slot in c.get("slots", []):
            sk = slot["slot_key"]
            if sk not in row:
                # Student wasn't on the roster for this slot
                self.att_tree.insert("", "end", iid=sk, values=(
                    slot.get("date", ""),
                    f"{slot.get('fromTime','')}–{slot.get('toTime','')}",
                    "—",
                    "—",
                ), tags=("not_enrolled",))
                continue

            pres = row[sk]
            is_staged = (pres != orig_row.get(sk, pres))
            p_cnt, a_cnt, t_cnt, is_taken = self._slot_stats(c, sk)
            pct = round(100 * p_cnt / t_cnt) if t_cnt else 0

            if is_taken:
                class_str = f"{p_cnt}P · {a_cnt}A ({pct}%)"
            else:
                class_str = "Untaken"

            if self._is_slot_dirty(c["key"], sk):
                class_str += " *"

            if is_staged:
                status_str = f"{'☑ present' if pres else '☐ absent'} *"
                tags = ("staged",)
            else:
                status_str = "☑ present" if pres else "☐ absent"
                tags = ()

            self.att_tree.insert("", "end", iid=sk, values=(
                slot.get("date", ""),
                f"{slot.get('fromTime','')}–{slot.get('toTime','')}",
                status_str,
                class_str,
            ), tags=tags)

        name = next((s["name"] for s in c["students"]
                     if s["admno"] == admno), admno)
        self.att_header.config(text=f"{name} ({admno})")
        self._update_att_summary()

    def _update_att_summary(self):
        c = self.course_by_key.get(self.selected_course_key)
        if not c or not self.selected_student_admno:
            return
        row = c["matrix"].get(self.selected_student_admno, {})
        p = sum(1 for v in row.values() if v)
        t = len(row)
        a = t - p
        pct = round(100 * p / t, 1) if t else 0
        self.att_summary.config(
            text=f"Present: {p} / {t}   ({pct}%)     Absent: {a}")

    # ---------- stage edits ----------

    def on_att_click(self, event):
        if self.att_tree.identify("region", event.x, event.y) != "cell":
            return
        if self.att_tree.identify_column(event.x) != "#3":
            return
        sk = self.att_tree.identify_row(event.y)
        if not sk:
            return

        c = self.course_by_key.get(self.selected_course_key)
        admno = self.selected_student_admno
        if not c or not admno:
            return
        slot = next((s for s in c["slots"] if s["slot_key"] == sk), None)
        if not slot:
            return
        row = c["matrix"].setdefault(admno, {})
        if sk not in row:
            return  # student not enrolled in this slot

        if not slot.get("taken") or slot.get("update_id") in (None, "", "0", 0):
            messagebox.showinfo(
                "Untaken Lecture",
                "Attendance has not been taken for this lecture yet.\n\n"
                "Please take attendance for the full class from the 'By Date' tab before editing individual students."
            )
            return

        new_value = not row[sk]
        row[sk] = new_value

        # Check if slot has staged differences from baseline
        if self._is_slot_dirty(c["key"], sk):
            self.dirty_slots.add((c["key"], sk))
        else:
            self.dirty_slots.discard((c["key"], sk))

        orig_row = c.get("orig_matrix", {}).get(admno, {})
        is_staged = (new_value != orig_row.get(sk, new_value))

        p_cnt, a_cnt, t_cnt, is_taken = self._slot_stats(c, sk)
        pct = round(100 * p_cnt / t_cnt) if t_cnt else 0
        class_str = f"{p_cnt}P · {a_cnt}A ({pct}%)" if is_taken else "Untaken"
        if self._is_slot_dirty(c["key"], sk):
            class_str += " *"

        status_str = f"{'☑ present' if new_value else '☐ absent'} *" if is_staged else ("☑ present" if new_value else "☐ absent")
        tags = ("staged",) if is_staged else ()

        self.att_tree.item(sk, values=(
            slot.get("date", ""),
            f"{slot.get('fromTime','')}–{slot.get('toTime','')}",
            status_str,
            class_str,
        ), tags=tags)

        self._update_att_summary()
        self._refresh_student_row(admno)
        self._refresh_course_row(c["key"])
        self._update_action_buttons()

    def _refresh_student_row(self, admno):
        c = self.course_by_key.get(self.selected_course_key)
        if not c:
            return
        row = c["matrix"].get(admno, {})
        p = sum(1 for v in row.values() if v)
        t = len(row)
        pct = round(100 * p / t) if t else 0
        orig_row = c.get("orig_matrix", {}).get(admno, {})
        is_stu_dirty = any(row.get(sk) != orig_row.get(sk) for sk in row)
        pct_str = f"{pct}% *" if is_stu_dirty else f"{pct}%"
        for s in c["students"]:
            if s["admno"] == admno:
                self.student_tree.item(
                    admno, values=(s["rollno"], s["name"], pct_str))
                break

    def _refresh_course_row(self, key):
        c = self.course_by_key.get(key)
        if not c:
            return
        p, a = self._course_totals(c)
        pct = round(100 * p / (p + a)) if (p + a) else 0
        label = c["subject"]
        if c.get("division"):
            label += f" · {c['division']}"
        if c.get("batch"):
            label += f" / {c['batch']}"
        if any(ck == key for ck, _ in self.dirty_slots):
            label += " *"
        self.course_tree.item(key, values=(label, p, a, f"{pct}%"))

    # ---------- batch submission & discard ----------

    def on_discard_changes(self):
        if not self.dirty_slots:
            return
        confirm = messagebox.askyesno(
            "Discard Changes",
            f"Discard all {self._count_total_staged_changes()} staged attendance change(s)?"
        )
        if not confirm:
            return

        for c in self.courses:
            c["matrix"] = {adm: dict(row) for adm, row in c.get("orig_matrix", {}).items()}
        self.dirty_slots.clear()

        self.on_course_selected()
        if self.selected_student_admno:
            self.on_student_selected()
        for c in self.courses:
            self._refresh_course_row(c["key"])
        self._update_action_buttons()
        self.app.set_status("Staged changes discarded.")

    def on_submit_changes(self):
        if not self.dirty_slots:
            return

        updates = []
        details = []
        for ck, sk in sorted(self.dirty_slots):
            c = self.course_by_key.get(ck)
            if not c:
                continue
            slot = next((s for s in c["slots"] if s["slot_key"] == sk), None)
            if not slot:
                continue
            present_admnos = [adm for adm, r in c["matrix"].items() if sk in r and r[sk]]
            p_cnt, a_cnt, t_cnt, _ = self._slot_stats(c, sk)
            updates.append({
                "entry": slot["entry"],
                "day_entries": slot["day_entries"],
                "present_admno": present_admnos,
                "expected_update_id": slot.get("update_id"),
                "force": False,
            })
            details.append(
                f"• {c['subject']} ({slot.get('date','')} {slot.get('fromTime','')}–{slot.get('toTime','')}): "
                f"{p_cnt} Pres, {a_cnt} Abs"
            )

        confirm_msg = (
            f"Submit attendance updates for {len(updates)} lecture(s)?\n\n"
            + "\n".join(details[:6])
        )
        if len(details) > 6:
            confirm_msg += f"\n... and {len(details) - 6} more lecture(s)"

        if not messagebox.askyesno("Submit Attendance Changes", confirm_msg):
            return

        self.submit_btn.config(state="disabled", text="Submitting…")
        self.discard_btn.config(state="disabled")
        self.app.set_status(f"Submitting attendance for {len(updates)} lecture(s)…")

        def do():
            return self.app.api.batch_update_slots(updates)

        def ok(data):
            results = data.get("results", [])
            success_count = 0
            errors = []

            for res in results:
                sk = res.get("slot_key")
                ok_flag = res.get("ok")
                if ok_flag:
                    success_count += 1
                    new_uid = res.get("new_update_id")
                    fresh_present = set(res.get("present_admno") or [])
                    # Find matching course and slot
                    for c in self.courses:
                        for slot in c.get("slots", []):
                            if slot["slot_key"] == sk:
                                slot["update_id"] = new_uid
                                for s in c["students"]:
                                    adm = s["admno"]
                                    if sk in c["matrix"].get(adm, {}):
                                        c["matrix"][adm][sk] = (adm in fresh_present)
                                    if sk in c["orig_matrix"].get(adm, {}):
                                        c["orig_matrix"][adm][sk] = (adm in fresh_present)
                                self.dirty_slots.discard((c["key"], sk))
                else:
                    err_msg = res.get("error") or "Unknown error"
                    errors.append(f"{sk}: {err_msg}")

            # Refresh views with verified server data
            self.on_course_selected()
            if self.selected_student_admno:
                self.on_student_selected()
            for c in self.courses:
                self._refresh_course_row(c["key"])
            self._update_action_buttons()

            if errors:
                err_text = "\n".join(errors[:4])
                if len(errors) > 4:
                    err_text += f"\n... and {len(errors) - 4} more error(s)"
                messagebox.showerror(
                    "Batch Submission Issue",
                    f"Saved {success_count} lecture(s), but {len(errors)} failed:\n\n{err_text}"
                )
                self.app.set_status(f"Completed with issues: {success_count} saved, {len(errors)} failed.")
            else:
                self.app.set_status(f"Saved {success_count} lecture(s) successfully. View refreshed.")

        def err(e):
            self._update_action_buttons()
            self.app.set_status("Batch submission failed.")
            messagebox.showerror(
                "Submission Error",
                getattr(e, "message", None) or str(e)
            )

        self.app.run_async(do, ok, err)
