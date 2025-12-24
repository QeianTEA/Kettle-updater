#!/usr/bin/env python3
"""
Kettle Updater - GUI (fixed):
- detects device (best-effort),
- downloads GitHub repo branch as zip,
- syncs files to the device using mpremote invoked IN-PROCESS (no spawning the exe),
- writes logs to C:\kettle_updater_logs\ (fallback to user home),
- shows a happy face on success.
"""

import os
import sys
import tempfile
import urllib.request
import zipfile
import shutil
import threading
import traceback
from datetime import datetime
from tkinter import Tk, Label, Entry, Button, StringVar, W, E, messagebox

# Optional nicer detection
try:
    import serial.tools.list_ports as list_ports
except Exception:
    list_ports = None

# For in-process mpremote invocation
import runpy
import io

# ----------------- Logging setup -----------------

def get_log_dir():
    """Prefer C:\kettle_updater_logs but fall back to user's home if not writable."""
    preferred = None
    if os.name == "nt":
        preferred = r"C:\kettle_updater_logs"
    else:
        preferred = os.path.join(os.path.expanduser("~"), ".kettle_updater_logs")

    try:
        os.makedirs(preferred, exist_ok=True)
        test_path = os.path.join(preferred, ".write_test")
        with open(test_path, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test_path)
        return preferred
    except Exception:
        fallback = os.path.join(os.path.expanduser("~"), ".kettle_updater_logs")
        os.makedirs(fallback, exist_ok=True)
        return fallback

LOG_DIR = get_log_dir()

def new_log_path(prefix="kettle_updater"):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{prefix}_{ts}.log"
    return os.path.join(LOG_DIR, fname)

def write_log(text, prefix="kettle_updater"):
    """
    Write `text` to a new log file and return the path.
    Robust fallback to temp dir if needed.
    """
    path = new_log_path(prefix)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path
    except Exception:
        # last-resort fallback in temp dir
        try:
            tmp = os.path.join(tempfile.gettempdir(), os.path.basename(path))
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            return tmp
        except Exception:
            return None

def append_to_log(path, text):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass

# ----------------- Helper functions -----------------

def detect_kettle_ports():
    """
    Best-effort detection of device-like serial ports.
    Returns list of port strings (Windows: COMx).
    """
    results = []
    if list_ports is None:
        return results
    try:
        for p in list_ports.comports():
            try:
                vid = p.vid
                pid = p.pid
            except Exception:
                vid = pid = None
            desc = " ".join(filter(None, [p.description, p.product, p.manufacturer])).lower()
            # Heuristics: common VID values and descriptive strings
            if vid is not None and vid in (0x2E8A, 0x1D50, 0x10C4, 0x0403):
                results.append(p.device)
                continue
            if any(k in desc for k in ("kettle", "pico", "raspberry", "rp2040", "usb serial", "usb-serial")):
                results.append(p.device)
                continue
    except Exception:
        return []
    return results

def download_github_zip(repo_url, branch="main"):
    """
    Download GitHub repo as zip and extract.
    Returns (tmpdir, repo_root).
    """
    if repo_url.endswith(".git"):
        repo_url = repo_url[:-4]
    if repo_url.startswith("git@github.com:"):
        repo_url = repo_url.replace("git@github.com:", "https://github.com/")
    repo_url = repo_url.rstrip("/")

    zip_url = repo_url + "/archive/refs/heads/" + branch + ".zip"
    tmpdir = tempfile.mkdtemp(prefix="kettle_updater_")
    zip_path = os.path.join(tmpdir, "repo.zip")
    try:
        with urllib.request.urlopen(zip_url) as resp, open(zip_path, "wb") as out:
            shutil.copyfileobj(resp, out)
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError(f"Failed to download repo zip: {e}\nTried URL: {zip_url}")

    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(tmpdir)
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError(f"Failed to extract repo zip: {e}")

    entries = [os.path.join(tmpdir, n) for n in os.listdir(tmpdir) if os.path.isdir(os.path.join(tmpdir, n))]
    if not entries:
        raise RuntimeError("Downloaded zip did not contain expected repository files.")
    repo_root = entries[0]
    return tmpdir, repo_root

def run_mpremote_inprocess(argv_list, status_callback=None, timeout_seconds=900):
    """
    Run mpremote in-process using runpy.run_module('mpremote').
    - argv_list should be like: ['mpremote','connect','auto','fs','sync','<local_path>',':']
    Returns (rc, output, logpath)
    rc: 0 success, nonzero error.
    """
    logpath = new_log_path("mpremote")
    # capture stdout/stderr
    old_argv = sys.argv[:]
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sio_out = io.StringIO()
    sio_err = io.StringIO()
    sys.argv = argv_list[:]  # mpremote reads sys.argv
    sys.stdout = sio_out
    sys.stderr = sio_err
    rc = 0
    try:
        # run the mpremote module as __main__ (this runs the CLI)
        runpy.run_module("mpremote", run_name="__main__")
    except SystemExit as se:
        # mpremote may call sys.exit(n)
        try:
            rc = int(se.code) if (se.code is not None and str(se.code).isdigit()) else (0 if se.code is None else 1)
        except Exception:
            rc = 1
    except Exception:
        rc = 1
        sio_err.write("\nUNCAUGHT EXCEPTION IN mpremote:\n")
        sio_err.write(traceback.format_exc())
    finally:
        # restore
        sys.argv = old_argv
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        out_text = sio_out.getvalue() + "\n" + sio_err.getvalue()
        try:
            with open(logpath, "w", encoding="utf-8") as f:
                f.write(f"COMMAND: {' '.join(argv_list)}\n\n")
                f.write(out_text)
        except Exception:
            pass
        return rc, out_text, logpath

def run_mpremote_sync(local_path, port=None, extra_args=None, status_callback=None):
    """
    High-level wrapper that prefers in-process invocation.
    Builds argv and calls run_mpremote_inprocess.
    """
    if extra_args is None:
        extra_args = []
    target = "auto" if port is None else port
    argv = ["mpremote", "connect", target, "fs", "sync", local_path, ":"] + extra_args
    if status_callback:
        status_callback(f"Invoking mpremote (in-process)...")
    # Try in-process first (works well when mpremote is bundled into exe as a module)
    try:
        rc, out, logpath = run_mpremote_inprocess(argv, status_callback=status_callback)
        return rc, out, logpath
    except Exception as e:
        # Fallback: try subprocess (may spawn exe if packaged; we log that attempt)
        fallback_log = new_log_path("mpremote_fallback")
        try:
            with open(fallback_log, "w", encoding="utf-8") as f:
                f.write("Falling back to subprocess invocation\n")
        except Exception:
            pass
        try:
            import subprocess
            cmd = [sys.executable, "-m", "mpremote"] + argv[1:]
            if status_callback:
                status_callback(f"Running fallback subprocess: {' '.join(cmd)}")
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900)
            out = proc.stdout or ""
            try:
                with open(fallback_log, "a", encoding="utf-8") as f:
                    f.write(out)
            except Exception:
                pass
            return proc.returncode, out, fallback_log
        except Exception as e2:
            msg = f"Both in-process and subprocess mpremote failed: {e}\nFallback error: {e2}"
            try:
                with open(fallback_log, "a", encoding="utf-8") as f:
                    f.write(msg + "\n" + traceback.format_exc())
            except Exception:
                pass
            return 1, msg, fallback_log

# ----------------- GUI -----------------

class KettleUpdaterGUI:
    def __init__(self, master):
        self.master = master
        master.title("Kettle Updater")
        master.resizable(False, False)

        Label(master, text="GitHub repo URL:").grid(row=0, column=0, sticky=W, padx=6, pady=6)
        self.repo_var = StringVar()
        Entry(master, textvariable=self.repo_var, width=48).grid(row=0, column=1, columnspan=2, padx=6)

        Label(master, text="Branch:").grid(row=1, column=0, sticky=W, padx=6)
        self.branch_var = StringVar(value="main")
        Entry(master, textvariable=self.branch_var, width=12).grid(row=1, column=1, sticky=W, padx=6)

        Label(master, text="Subfolder to sync (optional):").grid(row=2, column=0, sticky=W, padx=6)
        self.subfolder_var = StringVar(value="")
        Entry(master, textvariable=self.subfolder_var, width=36).grid(row=2, column=1, columnspan=2, padx=6)

        self.status_label = Label(master, text="Ready", anchor=W, width=60)
        self.status_label.grid(row=3, column=0, columnspan=3, sticky=W+E, padx=6, pady=(6,0))

        self.run_button = Button(master, text="Update Kettle", command=self.on_run)
        self.run_button.grid(row=4, column=1, pady=10)

        self.quit_button = Button(master, text="Quit", command=master.quit)
        self.quit_button.grid(row=4, column=2, pady=10, padx=(0,6))

    def set_status(self, text):
        def _set():
            self.status_label.config(text=text)
        self.master.after(0, _set)

    def show_error_box(self, title, msg):
        # ensure messagebox is called on main thread
        def _show():
            try:
                messagebox.showerror(title, msg)
            except Exception:
                pass
        self.master.after(0, _show)

    def on_run(self):
        repo = self.repo_var.get().strip()
        branch = self.branch_var.get().strip() or "main"
        subfolder = self.subfolder_var.get().strip().strip("/")
        if not repo:
            self.set_status("Please enter a GitHub repo URL (e.g. https://github.com/user/repo).")
            return
        # disable UI while running
        self.run_button.config(state="disabled")
        self.set_status("Starting update...")
        threading.Thread(target=self.worker, args=(repo, branch, subfolder), daemon=True).start()

    def worker(self, repo, branch, subfolder):
        tmpdir = None
        primary_log = new_log_path("session")
        try:
            append_to_log(primary_log, f"Start: {datetime.now().isoformat()}\n")
            self.set_status("Detecting device (best-effort)...")
            ports = detect_kettle_ports()
            if ports:
                porttxt = ", ".join(ports)
                self.set_status(f"Detected ports: {porttxt}. Using first: {ports[0]}")
                chosen_port = ports[0]
                append_to_log(primary_log, f"Detected ports: {porttxt}\n")
            else:
                self.set_status("No device-like serial port found. Will let mpremote auto-detect.")
                chosen_port = None
                append_to_log(primary_log, "No ports auto-detected; using mpremote auto\n")

            self.set_status("Downloading GitHub repo...")
            tmpdir, repo_root = download_github_zip(repo, branch)
            append_to_log(primary_log, f"Downloaded repo into: {tmpdir}\n")
            sync_path = repo_root
            if subfolder:
                candidate = os.path.join(repo_root, subfolder)
                if not os.path.isdir(candidate):
                    raise RuntimeError(f"Subfolder '{subfolder}' not found in the repo.")
                sync_path = candidate

            self.set_status("Syncing files to device (mpremote)...")
            rc, output, mp_log = run_mpremote_sync(sync_path, port=chosen_port, status_callback=self.set_status)
            append_to_log(primary_log, f"mpremote return code: {rc}\nmpremote log: {mp_log}\n")
            if rc == 0:
                self.set_status("😄 done!")
                append_to_log(primary_log, "Success!\n")
                write_log(f"Sync succeeded at {datetime.now().isoformat()}\nRepo: {repo}\nBranch: {branch}\n", prefix="success")
            else:
                out_text = f"mpremote rc={rc}\n\nmpremote output:\n{output}\n"
                append_to_log(primary_log, out_text)
                try:
                    with open(mp_log, "r", encoding="utf-8") as f:
                        append_to_log(primary_log, "\n=== mpremote full log ===\n")
                        append_to_log(primary_log, f.read())
                except Exception:
                    append_to_log(primary_log, f"\nCould not read mpremote log: {mp_log}\n")
                self.set_status(f"Error during sync. Log saved: {primary_log}")
                self.show_error_box("Kettle Updater", f"Sync failed. Log saved: {primary_log}")

        except Exception as e:
            tb = traceback.format_exc()
            append_to_log(primary_log, f"\nEXCEPTION:\n{tb}\n")
            errpath = write_log(f"Exception during run:\n\n{tb}\n", prefix="exception")
            self.set_status(f"Error: {e}. Log: {errpath or primary_log}")
            self.show_error_box("Kettle Updater - Error", f"An error occurred. Log: {errpath or primary_log}")
        finally:
            def _cleanup():
                try:
                    if tmpdir and os.path.isdir(tmpdir):
                        shutil.rmtree(tmpdir)
                except Exception:
                    pass
                self.run_button.config(state="normal")
            # restore UI quickly but keep logs around
            self.master.after(2000, _cleanup)

def main():
    root = Tk()
    gui = KettleUpdaterGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
