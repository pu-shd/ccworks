"""The setup window uses the same isolated login as the terminal CLI."""

from queue import Empty, Queue
from threading import Thread
import tkinter as tk
from tkinter import messagebox, ttk

from ccworks.desktop import BUSY, quiet_desktop, remove_data, setup_sign_in
from ccworks.win_update import WinSparkle


def main() -> None:
    quiet_desktop()
    root = tk.Tk()
    root.title("ccworks Desktop")
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=20)
    frame.grid()
    status = tk.StringVar(value="Sign in to connect ccworks to SAP Concur.")
    ttk.Label(
        frame, textvariable=status, wraplength=360, justify="left", width=49
    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 16))
    events: Queue[tuple[str, str]] = Queue()
    answer: Queue[bool] = Queue()
    phase = "idle"
    update_events: Queue[str] = Queue()

    try:
        updater = WinSparkle.from_bundle(update_events.put, lambda: phase == "updating")
    except (AttributeError, OSError, TypeError, ValueError, KeyError):
        updater = None
        status.set(
            "Windows updates are unavailable. Contact IT for an updated installer."
        )

    def submit(value: bool) -> None:
        nonlocal phase
        if phase not in {"opening", "waiting"}:
            return
        phase = "finishing"
        done.state(["disabled"])
        cancel.state(["disabled"])
        status.set("Verifying sign-in…" if value else "Cancelling sign-in…")
        answer.put(value)

    def poll() -> None:
        nonlocal phase
        try:
            action, message = events.get_nowait()
        except Empty:
            root.after(100, poll)
            return
        if action == "finished":
            phase = "idle"
            status.set(message)
            sign_in.state(["!disabled"])
            remove.state(["!disabled"])
            if updater:
                update.state(["!disabled"])
            done.state(["disabled"])
            cancel.state(["disabled"])
            return
        if action == "waiting" and phase == "opening":
            phase = "waiting"
            status.set(message)
            done.state(["!disabled"])
        root.after(100, poll)

    def sign_in_start() -> None:
        nonlocal phase
        phase = "opening"
        status.set("Opening your approved browser…")
        sign_in.state(["disabled"])
        remove.state(["disabled"])
        if updater:
            update.state(["disabled"])
        cancel.state(["!disabled"])

        def confirm() -> bool:
            events.put(
                (
                    "waiting",
                    "Complete SSO and MFA in the browser. When the Concur expense dashboard appears, return here and choose Done.",
                )
            )
            return answer.get()

        def worker() -> None:
            message = setup_sign_in(confirm)
            if not answer.empty():
                answer.get_nowait()
            events.put(("finished", message))

        Thread(target=worker).start()
        root.after(100, poll)

    def cleanup() -> None:
        if not messagebox.askyesno(
            "Remove ccworks data",
            "Remove the saved ccworks sign-in and local ccworks data?",
            parent=root,
        ):
            return
        try:
            remove_data()
        except TimeoutError:
            status.set(BUSY)
        except (OSError, ValueError):
            status.set(
                "ccworks data could not be removed. Contact IT and mention ccworks."
            )
        else:
            root.destroy()

    def poll_update() -> None:
        nonlocal phase
        while not update_events.empty():
            action = update_events.get_nowait()
            if action == "shutdown":
                root.destroy()
                return
            if action == "dismissed" and phase == "updating":
                phase = "idle"
                status.set("Sign in to connect ccworks to SAP Concur.")
                sign_in.state(["!disabled"])
                remove.state(["!disabled"])
                update.state(["!disabled"])
        root.after(100, poll_update)

    def check_for_updates() -> None:
        nonlocal phase
        if updater is None or phase != "idle":
            return
        phase = "updating"
        sign_in.state(["disabled"])
        remove.state(["disabled"])
        update.state(["disabled"])
        status.set("Check the Windows update window.")
        updater.check()

    def close() -> None:
        if phase in {"idle", "updating"}:
            root.destroy()
        else:
            submit(False)

    sign_in = ttk.Button(frame, text="Sign in to Concur", command=sign_in_start)
    sign_in.grid(row=1, column=0, sticky="w")
    done = ttk.Button(
        frame, text="Done", command=lambda: submit(True), state="disabled"
    )
    done.grid(row=1, column=1)
    cancel = ttk.Button(
        frame, text="Cancel", command=lambda: submit(False), state="disabled"
    )
    cancel.grid(row=1, column=2)
    remove = ttk.Button(frame, text="Remove ccworks data", command=cleanup)
    remove.grid(row=2, column=0, columnspan=3, sticky="w", pady=(14, 0))
    if updater:
        update = ttk.Button(frame, text="Check for Updates…", command=check_for_updates)
        update.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        root.after(100, poll_update)
    root.protocol("WM_DELETE_WINDOW", close)
    try:
        root.mainloop()
    finally:
        if updater:
            updater.close()
