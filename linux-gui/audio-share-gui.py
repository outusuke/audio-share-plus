#!/usr/bin/env python3
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

BINARY_NAME = "audioshare"
INSTALL_DIR = Path.home() / ".local/bin"
SERVICE = "audio-share"
UNIT_PATH = Path.home() / ".config/systemd/user" / f"{SERVICE}.service"
CONFIG_PATH = Path(GLib.get_user_config_dir()) / "audio-share-plus" / "gui.json"
DEFAULT_PORT = 65530
ENDPOINT_RE = re.compile(r"^\s*(\*)?\s*id:\s*(\d+)\s+name:\s*(.*?)\s*$")
PIPE_FLAGS = Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE
HAVE_SYSTEMD = shutil.which("systemctl") is not None

UNIT_TEMPLATE = """[Unit]
Description=Audio Share server
After=pipewire.service
StartLimitIntervalSec=0

[Service]
ExecStart={exec}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""

DEFAULTS = {
    "binary": shutil.which(BINARY_NAME) or BINARY_NAME,
    "host": "",
    "port": DEFAULT_PORT,
    "endpoint": "default",
    "compression": "opus",
    "bitrate": 128,
    "silence": 2,
    "discovery": False,
}
COMPRESSION = ["opus", "none"]


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def build_args(cfg):
    args = [shutil.which(cfg["binary"]) or cfg["binary"]]
    host = cfg["host"].strip()
    port = int(cfg["port"])
    if host:
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        args.append(f"--bind={host}:{port}")
    elif port != DEFAULT_PORT:
        raise ValueError("Set an address to use a custom port")
    else:
        args.append("-b")
    if cfg["endpoint"] != "default":
        args.append(f"--endpoint={cfg['endpoint']}")
    args.append(f"--compression={cfg['compression']}")
    if cfg["compression"] == "opus":
        args.append(f"--bitrate={cfg['bitrate']}")
    args.append(f"--silence-timeout={cfg['silence']}")
    if cfg["discovery"]:
        args.append("--discovery")
    return args


def list_endpoints(binary):
    try:
        out = subprocess.run([binary, "-l"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = []
    for line in out.splitlines():
        m = ENDPOINT_RE.match(line)
        if m:
            found.append((m.group(2), m.group(3), bool(m.group(1))))
    return found


def systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def service_enabled():
    return HAVE_SYSTEMD and systemctl("is-enabled", SERVICE).returncode == 0


def service_active():
    return HAVE_SYSTEMD and systemctl("is-active", SERVICE).returncode == 0


def write_unit(args):
    command = shlex.join(args).replace("%", "%%").replace("$", "$$")
    UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    UNIT_PATH.write_text(UNIT_TEMPLATE.format(exec=command))
    systemctl("daemon-reload")


class LineReader:
    def __init__(self, proc, on_line, on_done):
        self.on_line = on_line
        self.on_done = on_done
        self.stream = Gio.DataInputStream.new(proc.get_stdout_pipe())
        self.stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self.read)

    def read(self, stream, result):
        try:
            line, _ = stream.read_line_finish_utf8(result)
        except GLib.Error:
            line = None
        if line is None:
            self.on_done()
            return
        self.on_line(line)
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self.read)


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Audio Share Server")
        self.set_default_size(520, 620)
        self.cfg = load_config()
        self.proc = None
        self.follower = None
        self.endpoint_ids = ["default"]
        self.build_ui()
        self.refresh_endpoints()
        self.refresh_status()
        if self.autostart.get_active():
            self.follow_journal()
        GLib.timeout_add_seconds(2, self.refresh_status)
        self.connect("close-request", self.on_close)

    def build_ui(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(16)
        self.set_child(box)

        self.grid = Gtk.Grid(row_spacing=8, column_spacing=12)
        box.append(self.grid)
        self.row = 0

        self.binary = Gtk.Entry(hexpand=True)
        self.binary.set_text(self.cfg["binary"])
        install = Gtk.Button(label="Install…")
        install.connect("clicked", self.on_install)
        binary_box = Gtk.Box(spacing=6)
        binary_box.append(self.binary)
        binary_box.append(install)
        self.add_row("Server binary", binary_box)

        self.host = Gtk.Entry(hexpand=True, placeholder_text="Automatic")
        self.host.set_text(self.cfg["host"])
        self.add_row("Address", self.host)

        self.port = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.port.set_value(self.cfg["port"])
        self.add_row("Port", self.port)

        self.endpoint_model = Gtk.StringList()
        self.endpoint = Gtk.DropDown(model=self.endpoint_model, hexpand=True)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.connect("clicked", lambda _: self.refresh_endpoints())
        endpoint_box = Gtk.Box(spacing=6)
        endpoint_box.append(self.endpoint)
        endpoint_box.append(refresh)
        self.add_row("Audio endpoint", endpoint_box)

        self.compression = Gtk.DropDown.new_from_strings(["Opus", "Uncompressed PCM"])
        self.compression.set_selected(COMPRESSION.index(self.cfg["compression"]))
        self.compression.connect("notify::selected", self.sync_bitrate)
        self.add_row("Compression", self.compression)

        self.bitrate = Gtk.SpinButton.new_with_range(16, 510, 8)
        self.bitrate.set_value(self.cfg["bitrate"])
        self.add_row("Bitrate (kbit/s)", self.bitrate)
        self.sync_bitrate()

        self.silence = Gtk.SpinButton.new_with_range(0, 60, 1)
        self.silence.set_value(self.cfg["silence"])
        self.add_row("Silence timeout (s)", self.silence)

        self.discovery = Gtk.Switch(halign=Gtk.Align.START)
        self.discovery.set_active(self.cfg["discovery"])
        self.add_row("Let the app find this server", self.discovery)

        self.autostart = Gtk.Switch(halign=Gtk.Align.START, sensitive=HAVE_SYSTEMD)
        self.autostart.set_active(service_enabled())
        self.autostart.connect("notify::active", self.on_autostart)
        self.add_row("Start at login (systemd)", self.autostart)

        controls = Gtk.Box(spacing=12)
        self.button = Gtk.Button(label="Start")
        self.button.add_css_class("suggested-action")
        self.button.connect("clicked", self.on_toggle)
        self.status = Gtk.Label(xalign=0, hexpand=True)
        controls.append(self.button)
        controls.append(self.status)
        box.append(controls)

        self.log = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(self.log)
        box.append(scroll)

    def add_row(self, label, widget):
        self.grid.attach(Gtk.Label(label=label, xalign=0), 0, self.row, 1, 1)
        self.grid.attach(widget, 1, self.row, 1, 1)
        self.row += 1

    def on_install(self, _button):
        dialog = Gtk.FileDialog(title="Select the built server binary")
        dialog.open(self, None, self.on_install_chosen)

    def on_install_chosen(self, dialog, result):
        try:
            source = dialog.open_finish(result).get_path()
        except GLib.Error:
            return
        target = INSTALL_DIR / BINARY_NAME
        staging = target.with_name(BINARY_NAME + ".new")
        try:
            INSTALL_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, staging)
            staging.chmod(0o755)
            os.replace(staging, target)
        except OSError as e:
            self.append(f"Install failed: {e}")
            return
        self.binary.set_text(str(target))
        self.append(f"Installed {target}")
        self.refresh_endpoints()

    def sync_bitrate(self, *_):
        self.bitrate.set_sensitive(COMPRESSION[self.compression.get_selected()] == "opus")

    def refresh_endpoints(self):
        found = list_endpoints(self.binary.get_text().strip())
        if found is None:
            self.append(f"Could not run the server binary. Use Install… to copy it to {INSTALL_DIR}")
            found = []
        self.endpoint_ids = ["default"] + [i for i, _, _ in found]
        labels = ["Default"] + [f"{name} ({i})" for i, name, _ in found]
        self.endpoint_model.splice(0, self.endpoint_model.get_n_items(), labels)
        saved = self.cfg["endpoint"]
        self.endpoint.set_selected(self.endpoint_ids.index(saved) if saved in self.endpoint_ids else 0)

    def collect(self):
        return {
            "binary": self.binary.get_text().strip(),
            "host": self.host.get_text().strip(),
            "port": self.port.get_value_as_int(),
            "endpoint": self.endpoint_ids[self.endpoint.get_selected()],
            "compression": COMPRESSION[self.compression.get_selected()],
            "bitrate": self.bitrate.get_value_as_int(),
            "silence": self.silence.get_value_as_int(),
            "discovery": self.discovery.get_active(),
        }

    def append(self, line):
        buf = self.log.get_buffer()
        buf.insert(buf.get_end_iter(), line + "\n")
        if buf.get_line_count() > 1000:
            buf.delete(buf.get_start_iter(), buf.get_iter_at_line(200))
        mark = buf.create_mark(None, buf.get_end_iter(), False)
        self.log.scroll_mark_onscreen(mark)
        buf.delete_mark(mark)

    def refresh_status(self):
        service_up = self.autostart.get_active() and service_active()
        running = self.proc is not None or service_up
        self.button.set_label("Stop" if running else "Start")
        if service_up:
            self.status.set_text("Running as a systemd service")
        elif self.proc is not None:
            self.status.set_text("Running")
        else:
            self.status.set_text("Stopped")
        return GLib.SOURCE_CONTINUE

    def on_toggle(self, _button):
        if self.proc is not None or (self.autostart.get_active() and service_active()):
            self.stop()
        else:
            self.start()
        self.refresh_status()

    def start(self):
        cfg = self.collect()
        self.cfg = cfg
        save_config(cfg)
        try:
            args = build_args(cfg)
        except ValueError as e:
            self.append(str(e))
            return
        if self.autostart.get_active():
            write_unit(args)
            self.report(systemctl("restart", SERVICE))
            self.follow_journal()
            return
        try:
            self.proc = Gio.Subprocess.new(args, PIPE_FLAGS)
        except GLib.Error as e:
            self.append(e.message)
            return
        self.append("$ " + shlex.join(args))
        proc = self.proc
        LineReader(proc, self.append, lambda: self.on_exit(proc))

    def on_exit(self, proc):
        if self.proc is proc:
            self.proc = None
            self.append("Server stopped")
            self.refresh_status()

    def stop(self):
        if self.proc is not None:
            self.proc.send_signal(signal.SIGINT)
            GLib.timeout_add_seconds(5, self.force_stop, self.proc)
        else:
            self.report(systemctl("stop", SERVICE))

    def force_stop(self, proc):
        if self.proc is proc:
            proc.force_exit()
        return GLib.SOURCE_REMOVE

    def report(self, result):
        if result.returncode != 0:
            self.append(result.stderr.strip() or "systemctl failed")

    def follow_journal(self):
        if self.follower or not shutil.which("journalctl"):
            return
        argv = ["journalctl", "--user", "-u", SERVICE, "-n", "20", "-f", "-o", "cat"]
        try:
            self.follower = Gio.Subprocess.new(argv, PIPE_FLAGS)
        except GLib.Error:
            return
        LineReader(self.follower, self.append, lambda: setattr(self, "follower", None))

    def on_autostart(self, switch, _param):
        if switch.get_active():
            try:
                args = build_args(self.collect())
            except ValueError as e:
                self.append(str(e))
                switch.set_active(False)
                return
            if self.proc is not None:
                self.proc.send_signal(signal.SIGINT)
            write_unit(args)
            self.report(systemctl("enable", SERVICE))
            self.report(systemctl("restart", SERVICE))
            self.follow_journal()
        else:
            self.report(systemctl("disable", "--now", SERVICE))
            UNIT_PATH.unlink(missing_ok=True)
            systemctl("daemon-reload")
        self.refresh_status()

    def on_close(self, _window):
        save_config(self.collect())
        if self.proc is not None:
            self.proc.send_signal(signal.SIGINT)
        if self.follower is not None:
            self.follower.force_exit()
        return False


class App(Gtk.Application):
    def do_activate(self):
        Window(self).present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
