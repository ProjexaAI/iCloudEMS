"""iCloudEMS client UI (Tkinter).

Two tabs, both talking only to server/ over HTTP:
  - By date    — the original per-day take-attendance flow.
  - By course  — course-history view with three panes and per-student
                 attendance toggling.

This client is disposable: when the platform integrates, delete it and
point the platform at server/ using the same endpoints.
"""
import re
import threading
import uuid
from datetime import date, datetime
from tkinter import ttk, messagebox
import tkinter as tk

from .api import ServerClient, ServerError
from .courses_tab import CoursesTab


class AttendanceApp:
    def __init__(self, root):
        self.root = root
        self.root.title("iCloudEMS Attendance")
        self.root.geometry("1250x780")
        self.root.minsize(1050, 650)

        self.api = ServerClient()
        self.current_entries = []
        self.current_entry = None
        self.students = []
        self.update_id = None
        self.academicyear = None

        self._setup_style()
        self.container = ttk.Frame(self.root)
        self.container.pack(fill="both", expand=True)
        self.container.rowconfigure(0, weight=1)
        self.container.columnconfigure(0, weight=1)

        self._build_login_frame()
        self._build_otp_frame()
        self._build_dashboard_frame()

        self.show_login()

    # ---------- styling ----------

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#f5f7fa")
        style.configure("Card.TFrame", background="#ffffff", relief="flat")
        style.configure("Header.TFrame", background="#1e3a8a")
        style.configure("Header.TLabel", background="#1e3a8a",
                        foreground="white", font=("Segoe UI", 14, "bold"))
        style.configure("Sub.TLabel", background="#1e3a8a",
                        foreground="#cbd5e1", font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background="#f5f7fa",
                        foreground="#64748b", font=("Segoe UI", 10))
        style.configure("CardTitle.TLabel", background="#ffffff",
                        foreground="#0f172a", font=("Segoe UI", 12, "bold"))
        style.configure("TLabel", background="#f5f7fa", foreground="#0f172a",
                        font=("Segoe UI", 10))
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"),
                        padding=(16, 8))
        style.configure("Ghost.TButton", font=("Segoe UI", 10),
                        padding=(12, 6))
        style.configure("Treeview", font=("Segoe UI", 10), rowheight=26,
                        background="white", fieldbackground="white")
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("TNotebook", background="#f5f7fa", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 8),
                        font=("Segoe UI", 10, "bold"))

    def _clear_container(self):
        for w in self.container.winfo_children():
            w.pack_forget()

    # ---------- login ----------

    def _build_login_frame(self):
        self.login_frame = ttk.Frame(self.container)
        wrap = ttk.Frame(self.login_frame, style="Card.TFrame", padding=40)
        wrap.place(relx=0.5, rely=0.5, anchor="center")

        ttk.Label(wrap, text="iCloudEMS Attendance",
                  style="CardTitle.TLabel",
                  font=("Segoe UI", 20, "bold")).grid(row=0, column=0, pady=(0, 6))
        ttk.Label(wrap, text="Sign in to continue",
                  style="Muted.TLabel").grid(row=1, column=0, pady=(0, 24))

        ttk.Label(wrap, text="Email address").grid(row=2, column=0, sticky="w")
        self.email_var = tk.StringVar()
        email_entry = ttk.Entry(wrap, textvariable=self.email_var, width=36,
                                font=("Segoe UI", 11))
        email_entry.grid(row=3, column=0, pady=(4, 16), ipady=6)
        email_entry.bind("<Return>", lambda e: self.on_send_otp())
        self.email_entry = email_entry

        self.send_btn = ttk.Button(wrap, text="Continue",
                                   style="Primary.TButton",
                                   command=self.on_send_otp)
        self.send_btn.grid(row=4, column=0, pady=(4, 0), sticky="ew")

        self.login_status = ttk.Label(wrap, text="", style="Muted.TLabel",
                                      wraplength=360, justify="center")
        self.login_status.grid(row=5, column=0, pady=(16, 0))

        ttk.Label(wrap, text=f"Server: {self.api.base_url}",
                  style="Muted.TLabel",
                  font=("Segoe UI", 8)).grid(row=6, column=0, pady=(10, 0))

    def show_login(self):
        self._clear_container()
        self.login_status.config(text="")
        self.send_btn.config(state="normal", text="Continue")
        self.login_frame.pack(fill="both", expand=True)
        try:
            self.email_entry.focus_set()
        except Exception:
            pass

    # ---------- OTP ----------

    def _build_otp_frame(self):
        self.otp_frame = ttk.Frame(self.container)
        wrap = ttk.Frame(self.otp_frame, style="Card.TFrame", padding=40)
        wrap.place(relx=0.5, rely=0.5, anchor="center")

        ttk.Label(wrap, text="Enter OTP",
                  style="CardTitle.TLabel",
                  font=("Segoe UI", 20, "bold")).grid(row=0, column=0, pady=(0, 6))
        self.otp_hint = ttk.Label(wrap, text="", style="Muted.TLabel")
        self.otp_hint.grid(row=1, column=0, pady=(0, 24))

        ttk.Label(wrap, text="6-digit code").grid(row=2, column=0, sticky="w")
        self.otp_var = tk.StringVar()
        otp_entry = ttk.Entry(wrap, textvariable=self.otp_var, width=36,
                              font=("Segoe UI", 14), justify="center")
        otp_entry.grid(row=3, column=0, pady=(4, 16), ipady=6)
        otp_entry.bind("<Return>", lambda e: self.on_validate_otp())
        self.otp_entry = otp_entry

        btn_row = ttk.Frame(wrap, style="Card.TFrame")
        btn_row.grid(row=4, column=0, sticky="ew")
        for i in range(3):
            btn_row.columnconfigure(i, weight=1)
        ttk.Button(btn_row, text="Back", style="Ghost.TButton",
                   command=self.show_login
                   ).grid(row=0, column=0, padx=(0, 4), sticky="ew")
        ttk.Button(btn_row, text="Resend", style="Ghost.TButton",
                   command=self.on_resend_otp
                   ).grid(row=0, column=1, padx=4, sticky="ew")
        self.verify_btn = ttk.Button(btn_row, text="Verify",
                                     style="Primary.TButton",
                                     command=self.on_validate_otp)
        self.verify_btn.grid(row=0, column=2, padx=(4, 0), sticky="ew")

        self.otp_status = ttk.Label(wrap, text="", style="Muted.TLabel",
                                    wraplength=380, justify="center")
        self.otp_status.grid(row=5, column=0, pady=(16, 0))

    def show_otp(self):
        self._clear_container()
        self.otp_status.config(text="")
        self.verify_btn.config(state="normal", text="Verify")
        self.otp_hint.config(text=f"OTP sent to {self.api.email}")
        self.otp_var.set("")
        self.otp_frame.pack(fill="both", expand=True)
        try:
            self.otp_entry.focus_set()
        except Exception:
            pass

    # ---------- dashboard ----------

    def _build_dashboard_frame(self):
        self.dash_frame = ttk.Frame(self.container)

        header = ttk.Frame(self.dash_frame, style="Header.TFrame",
                           padding=(20, 14))
        header.pack(fill="x")
        ttk.Label(header, text="iCloudEMS Attendance",
                  style="Header.TLabel").pack(side="left")
        self.user_label = ttk.Label(header, text="", style="Sub.TLabel")
        self.user_label.pack(side="left", padx=(20, 0))

        ttk.Button(header, text="Forget", style="Ghost.TButton",
                   command=self.on_forget).pack(side="right", padx=(6, 0))
        ttk.Button(header, text="Refresh Tokens", style="Ghost.TButton",
                   command=self.on_refresh_tokens).pack(side="right", padx=(6, 0))
        ttk.Button(header, text="Logout", style="Ghost.TButton",
                   command=self.on_logout).pack(side="right")

        self.notebook = ttk.Notebook(self.dash_frame)
        self.notebook.pack(fill="both", expand=True, padx=16, pady=(16, 0))

        # Tab 1: by-date
        self.date_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.date_tab, text="By date")
        self._build_date_tab(self.date_tab)

        # Tab 2: by-course
        self.course_tab_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.course_tab_frame, text="By course")
        self.courses_tab = CoursesTab(self.course_tab_frame, self)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self.dash_frame, textvariable=self.status_var,
                  style="Muted.TLabel", padding=(16, 6)).pack(fill="x", side="bottom")

    def _build_date_tab(self, parent):
        body = ttk.Frame(parent, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1, minsize=380)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body, style="Card.TFrame", padding=16)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        ttk.Label(left, text="Classes", style="CardTitle.TLabel").pack(anchor="w")

        date_row = ttk.Frame(left, style="Card.TFrame")
        date_row.pack(fill="x", pady=(10, 8))
        ttk.Label(date_row, text="Date", style="Muted.TLabel").pack(side="left")
        self.date_var = tk.StringVar(value=date.today().isoformat())
        ttk.Entry(date_row, textvariable=self.date_var, width=12).pack(side="left", padx=8)
        self.load_btn = ttk.Button(date_row, text="Load", style="Primary.TButton",
                                   command=self.on_load_timetable)
        self.load_btn.pack(side="right")

        cols = ("time", "subject", "room", "division")
        self.class_tree = ttk.Treeview(left, columns=cols, show="headings", height=18)
        for c, w in (("time", 90), ("subject", 160), ("room", 70), ("division", 110)):
            self.class_tree.heading(c, text=c.capitalize())
            self.class_tree.column(c, width=w, anchor="w")
        self.class_tree.pack(fill="both", expand=True, pady=(8, 8))
        self.class_tree.bind("<<TreeviewSelect>>", self.on_class_selected)

        self.load_att_btn = ttk.Button(left, text="Load Attendance",
                                       style="Primary.TButton",
                                       state="disabled",
                                       command=self.on_load_attendance)
        self.load_att_btn.pack(fill="x")

        right = ttk.Frame(body, style="Card.TFrame", padding=16)
        right.grid(row=0, column=1, sticky="nsew")
        ttk.Label(right, text="Students", style="CardTitle.TLabel").pack(anchor="w")
        self.class_info_label = ttk.Label(right, text="Select a class on the left",
                                          style="Muted.TLabel")
        self.class_info_label.pack(anchor="w", pady=(2, 4))
        ttk.Label(
            right,
            text="Check the students who are PRESENT. "
                 "Unchecked rows are submitted as ABSENT.",
            style="Muted.TLabel", font=("Segoe UI", 9, "italic"),
        ).pack(anchor="w", pady=(0, 8))

        stu_cols = ("rollno", "name", "status")
        self.student_tree = ttk.Treeview(right, columns=stu_cols,
                                         show="headings", height=18)
        self.student_tree.heading("rollno", text="Roll No")
        self.student_tree.heading("name", text="Name")
        self.student_tree.heading("status", text="Present")
        self.student_tree.column("rollno", width=140, anchor="w")
        self.student_tree.column("name", width=300, anchor="w")
        self.student_tree.column("status", width=80, anchor="center")
        self.student_tree.pack(fill="both", expand=True, pady=(6, 10))
        self.student_tree.bind("<Button-1>", self._on_student_click)

        stu_actions = ttk.Frame(right, style="Card.TFrame")
        stu_actions.pack(fill="x")
        ttk.Button(stu_actions, text="Mark all present", style="Ghost.TButton",
                   command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(stu_actions, text="Mark all absent", style="Ghost.TButton",
                   command=lambda: self._set_all(False)).pack(side="left", padx=6)
        ttk.Button(stu_actions, text="Reload", style="Ghost.TButton",
                   command=self.on_load_attendance).pack(side="left", padx=6)
        self.submit_btn = ttk.Button(stu_actions, text="Submit Attendance",
                                     style="Primary.TButton",
                                     state="disabled",
                                     command=self.on_submit_attendance)
        self.submit_btn.pack(side="right")

    def show_dashboard(self):
        self._clear_container()
        self.user_label.config(
            text=f"{self.api.email or 'user'}  ·  EMP {self.api.empid or '?'}")
        self.status_var.set("Ready.")
        # Fresh session, clear any course-tab state from a previous login.
        try:
            self.courses_tab.reset()
        except Exception:
            pass
        self.dash_frame.pack(fill="both", expand=True)

    # ---------- threading ----------

    def run_async(self, fn, on_success=None, on_error=None):
        def worker():
            try:
                result = fn()
            except Exception as e:
                if on_error:
                    self.root.after(0, lambda e=e: on_error(e))
                return
            if on_success:
                self.root.after(0, lambda r=result: on_success(r))
        threading.Thread(target=worker, daemon=True).start()

    def set_status(self, text):
        self.status_var.set(text)

    # ---------- login handlers ----------

    def on_send_otp(self):
        email = self.email_var.get().strip()
        if not email or "@" not in email:
            messagebox.showwarning("Invalid email",
                                   "Please enter a valid email address.")
            return

        self.send_btn.config(state="disabled", text="Checking…")
        self.login_status.config(text="Contacting server…")

        def do():
            return self.api.start_login(email)

        def ok(data):
            self.send_btn.config(state="normal", text="Continue")
            if data.get("state") == "ready":
                self.login_status.config(text="Signed in from saved session.")
                self.show_dashboard()
                self.on_load_timetable()
            elif data.get("state") == "otp_sent":
                self.show_otp()
            else:
                self.login_status.config(text=f"Unexpected: {data}")

        def err(e):
            self.send_btn.config(state="normal", text="Continue")
            msg = getattr(e, "message", None) or str(e)
            self.login_status.config(text=msg[:300])

        self.run_async(do, ok, err)

    def on_validate_otp(self):
        otp = re.sub(r"\D", "", self.otp_var.get())
        if len(otp) < 4:
            messagebox.showwarning("Invalid OTP", "Enter the OTP (digits only).")
            return
        self.verify_btn.config(state="disabled", text="Verifying…")
        self.otp_status.config(text="Verifying…")

        def do():
            return self.api.verify_otp(otp)

        def ok(_data):
            self.verify_btn.config(state="normal", text="Verify")
            self.show_dashboard()
            self.set_status("Logged in. Loading timetable…")
            self.on_load_timetable()

        def err(e):
            self.verify_btn.config(state="normal", text="Verify")
            msg = getattr(e, "message", None) or str(e)
            self.otp_status.config(text=msg[:300])

        self.run_async(do, ok, err)

    def on_resend_otp(self):
        if not self.api.email:
            self.show_login()
            return
        self.otp_status.config(text="Requesting new OTP…")

        def do():
            return self.api.start_login(self.api.email)

        def ok(data):
            if data.get("state") == "otp_sent":
                self.otp_status.config(text=f"New OTP sent to {self.api.email}")
                self.otp_var.set("")
            else:
                self.otp_status.config(text=f"Unexpected: {data}")

        def err(e):
            self.otp_status.config(
                text=(getattr(e, "message", None) or str(e))[:300])

        self.run_async(do, ok, err)

    def on_refresh_tokens(self):
        self.set_status("Refreshing tokens…")

        def do():
            return self.api.refresh()

        def ok(data):
            exp = data.get("expires_in")
            if exp:
                self.set_status(f"Tokens refreshed — expires in ~{exp // 60} min.")
                messagebox.showinfo("Tokens",
                                    f"Refresh successful.\n\nExpires in ~{exp // 60} min.")
            else:
                self.set_status("Tokens refreshed.")
                messagebox.showinfo("Tokens", "Refresh successful.")

        def err(e):
            self.set_status("Token refresh failed.")
            messagebox.showerror("Refresh failed",
                                 getattr(e, "message", None) or str(e))

        self.run_async(do, ok, err)

    def on_logout(self):
        if not messagebox.askyesno(
                "Logout",
                "Log out of this session?\n\n"
                "(Saved tokens remain on the server — use 'Forget' to remove them.)"):
            return
        self.api.logout()
        self.current_entries = []
        self.current_entry = None
        self.students = []
        try:
            self.courses_tab.reset()
        except Exception:
            pass
        self.show_login()

    def on_forget(self):
        if not messagebox.askyesno(
                "Forget saved session",
                f"Remove saved tokens on the server for\n{self.api.email or '?'}?"):
            return
        self.api.forget_saved()
        self.api.logout()
        self.current_entries = []
        self.current_entry = None
        self.students = []
        try:
            self.courses_tab.reset()
        except Exception:
            pass
        self.show_login()

    # ---------- timetable (by date) ----------

    def on_load_timetable(self):
        target = self.date_var.get().strip()
        try:
            datetime.strptime(target, "%Y-%m-%d")
        except ValueError:
            messagebox.showwarning("Invalid date", "Use YYYY-MM-DD.")
            return

        self.load_btn.config(state="disabled", text="Loading…")
        self.set_status(f"Loading timetable for {target}…")
        self.class_tree.delete(*self.class_tree.get_children())
        self.student_tree.delete(*self.student_tree.get_children())
        self.current_entries = []
        self.current_entry = None
        self.students = []
        self.update_id = None
        self.submit_btn.config(state="disabled")
        self.load_att_btn.config(state="disabled")
        self.class_info_label.config(text="Select a class on the left")

        def do():
            return self.api.timetable(target)

        def ok(data):
            self.load_btn.config(state="normal", text="Load")
            self.current_entries = data.get("entries", [])
            for e in self.current_entries:
                self.class_tree.insert("", "end", values=(
                    f"{e.get('fromTime', '')}–{e.get('toTime', '')}",
                    e.get("subject_full") or e.get("sub_shortname") or "",
                    e.get("roomno", ""),
                    e.get("division", "")))
            if not self.current_entries:
                self.set_status(f"No classes found for {target}.")
            else:
                self.set_status(f"Loaded {len(self.current_entries)} class(es).")

        def err(e):
            self.load_btn.config(state="normal", text="Load")
            self.set_status("Failed to load timetable.")
            messagebox.showerror("Timetable error",
                                 getattr(e, "message", None) or str(e))

        self.run_async(do, ok, err)

    def on_class_selected(self, _event=None):
        sel = self.class_tree.selection()
        if not sel:
            self.current_entry = None
            self.load_att_btn.config(state="disabled")
            return
        idx = self.class_tree.index(sel[0])
        if 0 <= idx < len(self.current_entries):
            self.current_entry = self.current_entries[idx]
            self.load_att_btn.config(state="normal")
            e = self.current_entry
            self.class_info_label.config(
                text=f"{e.get('subject_full','')}  ·  {e.get('division','')}  ·  "
                     f"{e.get('fromTime','')}–{e.get('toTime','')}  ·  {e.get('roomno','')}")

    # ---------- roster (by date) ----------

    def on_load_attendance(self):
        if not self.current_entry:
            return
        entry = self.current_entry
        day_entries = self.current_entries
        self.load_att_btn.config(state="disabled", text="Loading…")
        self.submit_btn.config(state="disabled")
        self.set_status("Loading roster…")

        def do():
            return self.api.roster(entry, day_entries)

        def ok(data):
            self.load_att_btn.config(state="normal", text="Load Attendance")
            self.students = data.get("students", [])
            self.update_id = data.get("update_id")
            self.academicyear = (entry.get("acad_year")
                                 or self.academicyear
                                 or "2026-2027")

            self.student_tree.delete(*self.student_tree.get_children())
            if not self.students:
                self.set_status("Roster loaded, but no students were parsed.")
                return

            # If this is an untaken slot (no updateId and not marked taken),
            # default all students to PRESENT (☑), matching the mobile app behavior.
            is_taken = bool(data.get("taken_flag")) and bool(self.update_id)
            if not is_taken:
                for s in self.students:
                    s["present"] = True

            for s in self.students:
                self.student_tree.insert("", "end", values=(
                    s["rollno"], s["name"], "☑" if s["present"] else "☐"))

            present = sum(1 for s in self.students if s["present"])
            absent = len(self.students) - present
            tag = " (existing)" if is_taken else " (new - all marked present by default)"
            uid = f"  updateId={self.update_id}" if self.update_id else ""
            self.set_status(
                f"{len(self.students)} student(s) · "
                f"{present} present · {absent} absent{tag}{uid}")
            self.submit_btn.config(state="normal")

        def err(e):
            self.load_att_btn.config(state="normal", text="Load Attendance")
            self.set_status("Failed to load roster.")
            messagebox.showerror("Roster error",
                                 getattr(e, "message", None) or str(e))

        self.run_async(do, ok, err)

    def _on_student_click(self, event):
        if self.student_tree.identify("region", event.x, event.y) != "cell":
            return
        if self.student_tree.identify_column(event.x) != "#3":
            return
        item = self.student_tree.identify_row(event.y)
        if not item:
            return
        values = list(self.student_tree.item(item, "values"))
        values[2] = "☐" if values[2] == "☑" else "☑"
        self.student_tree.item(item, values=values)
        display = values[0]
        for s in self.students:
            if s["rollno"] == display:
                s["present"] = values[2] == "☑"
                break

    def _set_all(self, present):
        for s in self.students:
            s["present"] = present
        for item in self.student_tree.get_children():
            values = list(self.student_tree.item(item, "values"))
            values[2] = "☑" if present else "☐"
            self.student_tree.item(item, values=values)

    # ---------- submit (by date) ----------

    def on_submit_attendance(self):
        if not self.current_entry or not self.students:
            return

        present_adm = [s["admno"] for s in self.students if s["present"]]
        all_adm = [s["admno"] for s in self.students]
        absent_count = len(all_adm) - len(present_adm)

        force = False
        if len(all_adm) >= 10 and len(present_adm) <= 1:
            warn = messagebox.askyesno(
                "Warning: Low Presence Count",
                f"WARNING: Only {len(present_adm)} student(s) out of {len(all_adm)} are marked present!\n\n"
                f"Submitting will mark {absent_count} students as ABSENT.\n\n"
                f"Are you ABSOLUTELY sure this is intentional?"
            )
            if not warn:
                return
            force = True

        confirm = messagebox.askyesno(
            "Submit attendance",
            f"Submit attendance?\n\n"
            f"Present: {len(present_adm)}\n"
            f"Absent:  {absent_count}\n"
            f"updateId: {self.update_id or '<new>'}")
        if not confirm:
            return

        entry = self.current_entry
        key = uuid.uuid4().hex
        self.submit_btn.config(state="disabled", text="Submitting…")
        self.set_status("Submitting attendance…")

        def do():
            return self.api.submit(
                entry, all_adm, present_adm,
                self.update_id, self.academicyear, key, force=force,
            )

        def ok(_data):
            self.submit_btn.config(state="normal", text="Submit Attendance")
            self.set_status("Attendance submitted — reloading to verify…")
            self.on_load_attendance()

        def err(e):
            self.submit_btn.config(state="normal", text="Submit Attendance")
            self.set_status("Submission failed.")
            messagebox.showerror("Submit error",
                                 getattr(e, "message", None) or str(e))

        self.run_async(do, ok, err)


def main():
    root = tk.Tk()
    AttendanceApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
