#!/usr/bin/env python3
"""
Kettle Updater - GUI (with pre/post verification)
- Lists files on device before sync, syncs, lists after,
- Writes logs to C:\kettle_updater_logs\ (fallback to user home),
- Shows explicit success / no-changes / failure messages.
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
    path = new_log_path(prefix)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path
    except Exception:
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

# ----------------- Helpers -----------------

def detect_kettle_ports():
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
            if vid is not None and vid in (0x2E8A, 0x1D50, 0x10C4, 0x0403):
                results.append(p.device); continue
            if any(k in desc for k in ("kettle", "pico", "raspberry", "rp2040", "usb serial", "usb-serial")):
                results.append(p.device); continue
    except Exception:
        return []
    return results

def download_github_zip(repo_url, branch="main"):
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

# Run mpremote in-process and capture stdout/stderr
def run_mpremote_inprocess(argv_list):
    logpath = new_log_path("mpremote")
    old_argv = sys.argv[:]
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sio_out = io.StringIO()
    sio_err = io.StringIO()
    sys.argv = argv_list[:]
    sys.stdout = sio_out
    sys.stderr = sio_err
    rc = 0
    try:
        runpy.run_module("mpremote", run_name="__main__")
    except SystemExit as se:
        try:
            rc = int(se.code) if (se.code is not None and str(se.code).isdigit()) else (0 if se.code is None else 1)
        except Exception:
            rc = 1
    except Exception:
        rc = 1
        sio_err.write("\nUNCAUGHT EXCEPTION IN mpremote:\n")
        sio_err.write(traceback.format_exc())
    finally:
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
    if extra_args is None:
        extra_args = []
    target = "auto" if port is None else port
    argv = ["mpremote", "connect", target, "fs", "sync", local_path, ":"] + extra_args
    if status_callback:
        status_callback("Invoking mpremote (in-process) for sync...")
    rc, out, logpath = run_mpremote_inprocess(argv)
    return rc, out, logpath

def run_mpremote_ls(port=None):
    target = "auto" if port is None else port
    argv = ["mpremote", "connect", target, "fs", "ls", ":"]
    rc, out, logpath = run_mpremote_inprocess(argv)
    # produce a simple set of file entries from output lines
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    entries = set(lines)
    return rc, entries, out, logpath

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

    def show_info(self, title, msg):
        def _show():
            try:
                messagebox.showinfo(title, msg)
            except Exception:
                pass
        self.master.after(0, _show)

    def show_error(self, title, msg):
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
            chosen_port = ports[0] if ports else None
            append_to_log(primary_log, f"Detected ports: {ports}\nChosen: {chosen_port}\n")

            # pre-list files
            self.set_status("Listing files on device (before)...")
            rc_before, entries_before, out_before, before_log = run_mpremote_ls(port=chosen_port)
            append_to_log(primary_log, f"pre-list rc={rc_before}\nlog={before_log}\n")
            append_to_log(primary_log, out_before + "\n")

            self.set_status("Downloading GitHub repo...")
            tmpdir, repo_root = download_github_zip(repo, branch)
            append_to_log(primary_log, f"Downloaded repo into: {tmpdir}\n")
            sync_path = repo_root
            if subfolder:
                candidate = os.path.join(repo_root, subfolder)
                if not os.path.isdir(candidate):
                    raise RuntimeError(f"Subfolder '{subfolder}' not found in the repo.")
                sync_path = candidate

            # sync
            self.set_status("Syncing files to device (mpremote)...")
            rc_sync, out_sync, sync_log = run_mpremote_sync(sync_path, port=chosen_port)
            append_to_log(primary_log, f"sync rc={rc_sync}\nsync_log={sync_log}\n")
            append_to_log(primary_log, out_sync + "\n")

            # post-list files
            self.set_status("Listing files on device (after)...")
            rc_after, entries_after, out_after, after_log = run_mpremote_ls(port=chosen_port)
            append_to_log(primary_log, f"post-list rc={rc_after}\nlog={after_log}\n")
            append_to_log(primary_log, out_after + "\n")

            # Diff
            added = sorted(list(entries_after - entries_before))
            removed = sorted(list(entries_before - entries_after))
            common = sorted(list(entries_before & entries_after))

            # Interpret results
            if rc_sync == 0 and (len(added) > 0 or len(removed) > 0):
                msg = (f"Sync reported success.\nFiles added: {len(added)}\nFiles removed: {len(removed)}\n"
                       f"See logs in:\n{primary_log}\n{sync_log}\n{before_log}\n{after_log}")
                self.set_status("😄 done! Files changed.")
                append_to_log(primary_log, "Result: files changed.\n")
                self.show_info("Kettle Updater - Done", msg)
            elif rc_sync == 0 and (len(added) == 0 and len(removed) == 0):
                # no changed files
                msg = ("Sync completed but no files changed on the device.\n\n"
                       "Possible reasons:\n"
                       "• The device already had the same files (no update required).\n"
                       "• You selected the wrong subfolder in the repo (check 'Subfolder to sync').\n"
                       "• The repo branch you selected is empty/different.\n\n"
                       "Check these logs for details:\n"
                       f"{primary_log}\n{sync_log}\n{before_log}\n{after_log}")
                self.set_status("Done — no changes detected.")
                append_to_log(primary_log, "Result: no changes detected.\n")
                self.show_info("Kettle Updater - No changes", msg)
            else:
                # sync reported nonzero return code or other error
                msg = (f"Sync may have failed (rc={rc_sync}).\nCheck logs:\n{primary_log}\n{sync_log}\n{before_log}\n{after_log}\n\n"
                       "If mpremote couldn't find the device, check cable/driver or try holding BOOTSEL and replugging.")
                self.set_status("Error during sync. See logs.")
                append_to_log(primary_log, f"Sync reported failure rc={rc_sync}\n")
                self.show_error("Kettle Updater - Error", msg)

        except Exception as e:
            tb = traceback.format_exc()
            append_to_log(primary_log, f"\nEXCEPTION:\n{tb}\n")
            errpath = write_log(f"Exception during run:\n\n{tb}\n", prefix="exception")
            self.set_status(f"Error: {e}. Log: {errpath or primary_log}")
            self.show_error("Kettle Updater - Error", f"An error occurred. Log: {errpath or primary_log}")
        finally:
            def _cleanup():
                try:
                    if tmpdir and os.path.isdir(tmpdir):
                        shutil.rmtree(tmpdir)
                except Exception:
                    pass
                self.run_button.config(state="normal")
            self.master.after(2000, _cleanup)

def main():
    root = Tk()
    gui = KettleUpdaterGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
