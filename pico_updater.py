#!/usr/bin/env python3
"""
Kettle Updater - GUI:
- detects Kettle (best-effort),
- downloads GitHub repo branch as zip,
- syncs files to the Kettle using mpremote (invoked as a module),
- writes logs to C:\kettle_updater_logs\ (fallback to user home),
- shows a happy face on success.

Save as kettle_updater.py
"""

import os
import sys
import tempfile
import urllib.request
import zipfile
import shutil
import subprocess
import threading
import time
import traceback
from datetime import datetime
from tkinter import Tk, Label, Entry, Button, StringVar, W, E, messagebox

# Optional nicer detection
try:
    import serial.tools.list_ports as list_ports
except Exception:
    list_ports = None

# ----------------- Logging setup -----------------

def get_log_dir():
    """Prefer C:\kettle_updater_logs but fall back to user's home if not writable."""
    preferred = None
    if os.name == "nt":
        preferred = r"C:\kettle_updater_logs"
    else:
        # non-windows fallback
        preferred = os.path.join(os.path.expanduser("~"), ".kettle_updater_logs")

    try:
        os.makedirs(preferred, exist_ok=True)
        # test write permission
        test_path = os.path.join(preferred, ".write_test")
        with open(test_path, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test_path)
        return preferred
    except Exception:
        # fallback to user home
        fallback = os.path.join(os.path.expanduser("~"), ".kettle_updater_logs")
        os.makedirs(fallback, exist_ok=True)
        return fallback

LOG_DIR = get_log_dir()

def new_log_path(prefix="kettle_updater"):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{prefix}_{ts}.log"
    return os.path.join(LOG_DIR, fname)

def write_log(text, prefix="kettle_updater"):
    path = new_log_path(prefix)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        # last resort: write to temp dir
        tmp = os.path.join(tempfile.gettempdir(), fname)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            return tmp
        except Exception:
            return None
    return path

def append_to_log(path, text):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass

# ----------------- Helper functions -----------------

def detect_kettle_ports():
    """
    Best-effort detection of kettle-like serial ports.
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
            # Heuristics - common vendor id for Raspberry Pi is 0x2E8A but clones vary.
            if vid is not None and vid in (0x2E8A, 0x2e8a, 0x1d50, 0x10c4, 0x0403):
                results.append(p.device)
                continue
            if any(k in desc for k in ("kettle", "raspberry", "rp2040", "usb serial", "usb-serial")):
                results.append(p.device)
                continue
    except Exception:
        # If detection fails, return empty and let mpremote auto-detect
        return []
    return results

def download_github_zip(repo_url, branch="main"):
    """
    Download GitHub repo as zip and extract.
    Returns (tmpdir, repo_root).
    """
    # Normalize
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

def run_mpremote_sync(local_path, port=None, extra_args=None, status_callback=None):
    """
    Run mpremote as a module: python -m mpremote ...
    Returns (rc, full_output, logpath).
    """
    if extra_args is None:
        extra_args = []
    target = "auto" if port is None else port
    # Use sys.executable -m mpremote so mpremote is invoked as a module
    cmd = [sys.executable, "-m", "mpremote", "connect", target, "fs", "sync", local_path, ":"] + extra_args
    if status_callback:
        status_callback(f"Running: {' '.join(cmd)}")
    logpath = new_log_path("mpremote")
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900)
        out = proc.stdout or ""
        # Always write the output to a log file for debugging
        try:
            with open(logpath, "w", encoding="utf-8") as f:
                f.write(f"COMMAND: {' '.join(cmd)}\n\n")
                f.write(out)
        except Exception:
            pass
        return proc.returncode, out, logpath
    except FileNotFoundError as e:
        msg = ("mpremote not found when invoking as module. Make sure mpremote is installed "
               "in the same Python environment used to build the exe. (pip install mpremote)")
        try:
            with open(logpath, "w", encoding="utf-8") as f:
                f.write(msg + "\n\n" + traceback.format_exc())
        except Exception:
            pass
        return 127, msg + "\n" + str(e), logpath
    except subprocess.TimeoutExpired as e:
        out = getattr(e, "output", "") or ""
        try:
            with open(logpath, "w", encoding="utf-8") as f:
                f.write("mpremote timed out\n\n")
                f.write(out)
        except Exception:
            pass
        return 124, "mpremote timed out", logpath
    except Exception as e:
        try:
            with open(logpath, "w", encoding="utf-8") as f:
                f.write("Unexpected error running mpremote:\n")
                f.write(traceback.format_exc())
        except Exception:
            pass
        return 1, f"Unexpected error: {e}", logpath

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

    def on_run(self):
        repo = self.repo_var.get().strip()
        branch = self.branch_var.get().strip() or "main"
        subfolder = self.subfolder_var.get().strip().strip("/")
        if not repo:
            self.set_status("Please enter a GitHub repo URL (e.g. https://github.com/user/repo).")
            return
        self.run_button.config(state="disabled")
        self.set_status("Starting update...")
        threading.Thread(target=self.worker, args=(repo, branch, subfolder), daemon=True).start()

    def worker(self, repo, branch, subfolder):
        tmpdir = None
        primary_log = new_log_path("session")
        try:
            append_to_log(primary_log, f"Start: {datetime.now().isoformat()}\n")
            self.set_status("Detecting Kettle (best-effort)...")
            ports = detect_kettle_ports()
            if ports:
                porttxt = ", ".join(ports)
                self.set_status(f"Detected ports: {porttxt}. Using first: {ports[0]}")
                chosen_port = ports[0]
                append_to_log(primary_log, f"Detected ports: {porttxt}\n")
            else:
                self.set_status("No Kettle-like serial port found. Will let mpremote auto-detect.")
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

            self.set_status("Syncing files to Kettle...")
            rc, output, mp_log = run_mpremote_sync(sync_path, port=chosen_port, status_callback=self.set_status)
            append_to_log(primary_log, f"mpremote return code: {rc}\nmpremote log: {mp_log}\n")
            if rc == 0:
                self.set_status("😄 done!")
                append_to_log(primary_log, "Success!\n")
                # Write a short success log
                write_log(f"Sync succeeded at {datetime.now().isoformat()}\nRepo: {repo}\nBranch: {branch}\n", prefix="success")
            else:
                # Save both mpremote log and session log for reporting
                out_text = f"mpremote rc={rc}\n\nmpremote output:\n{output}\n"
                append_to_log(primary_log, out_text)
                # Copy mp_log to primary log folder as well
                try:
                    with open(mp_log, "r", encoding="utf-8") as f:
                        append_to_log(primary_log, "\n=== mpremote full log ===\n")
                        append_to_log(primary_log, f.read())
                except Exception:
                    append_to_log(primary_log, f"\nCould not read mpremote log: {mp_log}\n")
                self.set_status(f"Error during sync. Log saved: {primary_log}")
                # Also show a message box to alert
                try:
                    messagebox.showerror("Kettle Updater", f"Sync failed. Log saved: {primary_log}")
                except Exception:
                    pass

        except Exception as e:
            tb = traceback.format_exc()
            append_to_log(primary_log, f"\nEXCEPTION:\n{tb}\n")
            # try to save detailed trace to a separate file too
            errpath = write_log(f"Exception during run:\n\n{tb}\n", prefix="exception")
            self.set_status(f"Error: {e}. Log: {errpath or primary_log}")
            try:
                messagebox.showerror("Kettle Updater - Error", f"An error occurred. Log: {errpath or primary_log}")
            except Exception:
                pass
        finally:
            def _cleanup():
                try:
                    if tmpdir and os.path.isdir(tmpdir):
                        shutil.rmtree(tmpdir)
                except Exception:
                    pass
                self.run_button.config(state="normal")
            # keep logs accessible for a short while, then cleanup
            self.master.after(2000, _cleanup)

def main():
    root = Tk()
    gui = KettleUpdaterGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
