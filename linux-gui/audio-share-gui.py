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
SOURCE_NAME = "as-cmd"
BUILD_DIRS = [
    Path(__file__).resolve().parent.parent / "server-core/out/install/linux-Release/bin",
    Path(__file__).resolve().parent.parent / "server-core/out/build/linux-Release",
]
INSTALL_DIR = Path.home() / ".local/bin"
APP_ID = "io.github.mkckr0.AudioShareGui"
GUI_NAME = "audioshare-gui"
ICON_NAME = "audioshare"
ICON_SOURCE = Path(__file__).resolve().parent.parent / "metadata/en-US/images/icon.png"
DATA_DIR = Path(GLib.get_user_data_dir())
DESKTOP_PATH = DATA_DIR / "applications" / f"{APP_ID}.desktop"
ICON_PATH = DATA_DIR / "icons/hicolor/512x512/apps" / f"{ICON_NAME}.png"
SERVICE = "audio-share"
UNIT_PATH = Path.home() / ".config/systemd/user" / f"{SERVICE}.service"
CONFIG_PATH = Path(GLib.get_user_config_dir()) / "audio-share-plus" / "gui.json"
DEFAULT_PORT = 65530
ENDPOINT_RE = re.compile(r"^\s*(\*)?\s*id:\s*(\d+)\s+name:\s*(.*?)\s*$")
PIPE_FLAGS = Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE
HAVE_SYSTEMD = shutil.which("systemctl") is not None

DESKTOP_TEMPLATE = """[Desktop Entry]
Type=Application
Name=Audio Share Server
Comment=Stream this computer's audio to your phone
Exec={exec}
Icon={icon}
Terminal=false
Categories=AudioVideo;Audio;Network;
Keywords=audio;stream;share;server;
StartupNotify=true
"""

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
    "binary": shutil.which(BINARY_NAME) or shutil.which(SOURCE_NAME) or BINARY_NAME,
    "host": "",
    "port": DEFAULT_PORT,
    "endpoint": "default",
    "compression": "opus",
    "bitrate": 128,
    "silence": 2,
    "discovery": False,
}
COMPRESSION = ["opus", "none"]


def find_source():
    candidates = [shutil.which(SOURCE_NAME)] + [d / SOURCE_NAME for d in BUILD_DIRS]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


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


def run_systemctl(args, done):
    flags = Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_PIPE
    try:
        proc = Gio.Subprocess.new(["systemctl", "--user", *args], flags)
    except GLib.Error as e:
        done(1, e.message)
        return

    def finished(p, result):
        try:
            _ok, _out, err = p.communicate_utf8_finish(result)
        except GLib.Error as e:
            done(1, e.message)
            return
        done(p.get_exit_status(), (err or "").strip())

    proc.communicate_utf8_async(None, None, finished)


def launcher_paths():
    return [INSTALL_DIR / GUI_NAME, DESKTOP_PATH, ICON_PATH]


def refresh_desktop_caches():
    for argv in (
        ["update-desktop-database", str(DESKTOP_PATH.parent)],
        ["gtk-update-icon-cache", "-f", "-t", str(DATA_DIR / "icons/hicolor")],
    ):
        if shutil.which(argv[0]):
            subprocess.run(argv, capture_output=True)


def install_launcher():
    script = Path(__file__).resolve()
    gui = INSTALL_DIR / GUI_NAME
    if gui.resolve() != script:
        staging = gui.with_name(GUI_NAME + ".new")
        shutil.copy2(script, staging)
        staging.chmod(0o755)
        os.replace(staging, gui)
    icon = "audio-card"  # the repo icon is missing when run from the installed copy
    if ICON_SOURCE.is_file():
        ICON_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ICON_SOURCE, ICON_PATH)
        icon = ICON_NAME
    exec_path = str(gui).replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    DESKTOP_PATH.parent.mkdir(parents=True, exist_ok=True)
    DESKTOP_PATH.write_text(DESKTOP_TEMPLATE.format(exec=f'"{exec_path}"'.replace("%", "%%"), icon=icon))
    refresh_desktop_caches()
    return gui


def remove_launcher():
    for path in launcher_paths():
        path.unlink(missing_ok=True)
    refresh_desktop_caches()


def write_unit(args):
    command = shlex.join(args).replace("%", "%%").replace("$", "$$")
    UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    UNIT_PATH.write_text(UNIT_TEMPLATE.format(exec=command))


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
        self.service_up = False
        self.polling = False
        self.busy = False
        self.busy_label = ""
        self.stopping = False
        self.after_exit = None
        self.apply_id = 0
        self.quiet = 1
        self.build_ui()
        self.refresh_endpoints()
        self.quiet = 0
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
        self.uninstall_button = Gtk.Button(label="Uninstall…")
        self.uninstall_button.connect("clicked", self.on_uninstall)
        binary_box.append(self.uninstall_button)
        self.add_row("Server binary", binary_box)

        self.host = Gtk.Entry(hexpand=True, placeholder_text="Automatic")
        self.host.set_text(self.cfg["host"])
        self.host.connect("notify::text", self.schedule_apply)
        self.add_row("Address", self.host)

        self.port = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.port.set_value(self.cfg["port"])
        self.port.connect("value-changed", self.schedule_apply)
        self.add_row("Port", self.port)

        self.endpoint_model = Gtk.StringList()
        self.endpoint = Gtk.DropDown(model=self.endpoint_model, hexpand=True)
        self.endpoint.connect("notify::selected", self.schedule_apply)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.connect("clicked", lambda _: self.refresh_endpoints())
        endpoint_box = Gtk.Box(spacing=6)
        endpoint_box.append(self.endpoint)
        endpoint_box.append(refresh)
        self.add_row("Audio endpoint", endpoint_box)

        self.compression = Gtk.DropDown.new_from_strings(["Opus", "Uncompressed PCM"])
        self.compression.set_selected(COMPRESSION.index(self.cfg["compression"]))
        self.compression.connect("notify::selected", self.sync_bitrate)
        self.compression.connect("notify::selected", self.schedule_apply)
        self.add_row("Compression", self.compression)

        self.bitrate = Gtk.SpinButton.new_with_range(16, 510, 8)
        self.bitrate.set_value(self.cfg["bitrate"])
        self.bitrate.connect("value-changed", self.schedule_apply)
        self.add_row("Bitrate (kbit/s)", self.bitrate)
        self.sync_bitrate()

        self.silence = Gtk.SpinButton.new_with_range(0, 60, 1)
        self.silence.set_value(self.cfg["silence"])
        self.silence.connect("value-changed", self.schedule_apply)
        self.add_row("Silence timeout (s)", self.silence)

        self.discovery = Gtk.Switch(halign=Gtk.Align.START)
        self.discovery.set_active(self.cfg["discovery"])
        self.discovery.connect("notify::active", self.schedule_apply)
        self.add_row("Let the app find this server", self.discovery)

        self.autostart = Gtk.Switch(halign=Gtk.Align.START, sensitive=HAVE_SYSTEMD)
        self.autostart.set_active(service_enabled())
        self.autostart.connect("notify::active", self.on_autostart)
        self.add_row("Start at login (systemd)", self.autostart)

        controls = Gtk.Box(spacing=12)
        self.start_button = Gtk.Button(label="Start")
        self.start_button.add_css_class("suggested-action")
        self.start_button.connect("clicked", self.on_start)
        self.stop_button = Gtk.Button(label="Stop")
        self.stop_button.add_css_class("destructive-action")
        self.stop_button.connect("clicked", self.on_stop)
        self.status = Gtk.Label(xalign=0, hexpand=True)
        controls.append(self.start_button)
        controls.append(self.stop_button)
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
        source = find_source()
        if source:
            self.install_from(source)
            return
        dialog = Gtk.FileDialog(title=f"Select the built {SOURCE_NAME} binary")
        dialog.open(self, None, self.on_install_chosen)

    def on_install_chosen(self, dialog, result):
        try:
            source = dialog.open_finish(result).get_path()
        except GLib.Error:
            return
        self.install_from(source)

    def install_from(self, source):
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
        self.append(f"Installed {source} as {target}")
        try:
            gui = install_launcher()
            self.append(f"Added Audio Share Server to the app menu ({gui})")
        except OSError as e:
            self.append(f"App menu entry failed: {e}")
        self.refresh_endpoints()

    def sync_bitrate(self, *_):
        self.bitrate.set_sensitive(COMPRESSION[self.compression.get_selected()] == "opus")

    def refresh_endpoints(self):
        self.quiet += 1
        found = list_endpoints(self.binary.get_text().strip())
        if found is None:
            self.append(f"Could not run the server binary. Install… copies {SOURCE_NAME} to {INSTALL_DIR / BINARY_NAME}")
            found = []
        self.endpoint_ids = ["default"] + [i for i, _, _ in found]
        labels = ["Default"] + [f"{name} ({i})" for i, name, _ in found]
        self.endpoint_model.splice(0, self.endpoint_model.get_n_items(), labels)
        saved = self.cfg["endpoint"]
        self.endpoint.set_selected(self.endpoint_ids.index(saved) if saved in self.endpoint_ids else 0)
        self.quiet -= 1

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

    def is_running(self):
        return self.proc is not None or self.service_up

    def update_controls(self):
        running = self.is_running()
        working = self.busy or self.stopping
        self.start_button.set_sensitive(not running and not working)
        self.stop_button.set_sensitive(running and not working)
        self.uninstall_button.set_sensitive(
            not working and ((INSTALL_DIR / BINARY_NAME).exists() or UNIT_PATH.exists() or DESKTOP_PATH.exists())
        )
        if self.busy:
            self.status.set_text(self.busy_label)
        elif self.stopping:
            self.status.set_text("Stopping…")
        elif self.service_up:
            self.status.set_text("Running as a systemd service")
        elif self.proc is not None:
            self.status.set_text("Running")
        else:
            self.status.set_text("Stopped")

    def refresh_status(self):
        self.poll_service()
        return GLib.SOURCE_CONTINUE

    def poll_service(self):
        if not self.autostart.get_active():
            self.service_up = False
            self.update_controls()
            return
        if self.polling:
            return
        self.polling = True

        def finished(code, _err):
            self.polling = False
            self.service_up = code == 0
            self.update_controls()

        run_systemctl(["is-active", SERVICE], finished)

    def run_steps(self, steps, label, done=None):
        self.busy = True
        self.busy_label = label
        self.update_controls()

        def step(i):
            if i == len(steps):
                self.busy = False
                self.poll_service()
                if done:
                    done()
                return
            if callable(steps[i]):
                steps[i]()
                step(i + 1)
                return

            def finished(code, err):
                if code != 0:
                    self.append(err or "systemctl failed")
                step(i + 1)

            run_systemctl(steps[i], finished)

        step(0)

    def set_autostart(self, active):
        self.quiet += 1
        self.autostart.set_active(active)
        self.quiet -= 1

    def schedule_apply(self, *_):
        if self.quiet or not self.is_running():
            return
        if self.apply_id:
            GLib.source_remove(self.apply_id)
        self.apply_id = GLib.timeout_add(800, self.apply_now)

    def apply_now(self):
        self.apply_id = 0
        if not self.is_running():
            return GLib.SOURCE_REMOVE
        if self.busy or self.stopping:
            self.apply_id = GLib.timeout_add(500, self.apply_now)
            return GLib.SOURCE_REMOVE
        try:
            build_args(self.collect())
        except ValueError as e:
            self.append(str(e))
            return GLib.SOURCE_REMOVE
        self.append("Settings changed, restarting the server")
        self.stop_proc(then=self.start)
        return GLib.SOURCE_REMOVE

    def on_start(self, _button):
        self.start()
        self.update_controls()

    def on_stop(self, _button):
        self.stop()
        self.update_controls()

    def on_uninstall(self, _button):
        dialog = Gtk.AlertDialog(
            message="Uninstall Audio Share?",
            detail=f"This stops the server, removes the systemd service and deletes {INSTALL_DIR / BINARY_NAME} and the app menu entry. Your settings are kept.",
            buttons=["Cancel", "Uninstall"],
            cancel_button=0,
            default_button=0,
        )
        dialog.choose(self, None, self.on_uninstall_chosen)

    def on_uninstall_chosen(self, dialog, result):
        try:
            if dialog.choose_finish(result) != 1:
                return
        except GLib.Error:
            return

        def remove_files():
            (INSTALL_DIR / BINARY_NAME).unlink(missing_ok=True)
            remove_launcher()
            self.append("Uninstalled")

        steps = [remove_files]
        if HAVE_SYSTEMD:
            steps = [
                ["disable", "--now", SERVICE],
                lambda: UNIT_PATH.unlink(missing_ok=True),
                ["daemon-reload"],
                remove_files,
            ]
        self.set_autostart(False)
        self.stop_proc(then=lambda: self.run_steps(steps, "Uninstalling…"))

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
            self.run_steps([["daemon-reload"], ["restart", SERVICE]], "Starting…", self.follow_journal)
            return
        try:
            self.proc = Gio.Subprocess.new(args, PIPE_FLAGS)
        except GLib.Error as e:
            self.append(e.message)
            return
        self.append("$ " + shlex.join(args))
        proc = self.proc
        LineReader(proc, self.append, lambda: self.on_exit(proc))
        self.update_controls()

    def on_exit(self, proc):
        if self.proc is not proc:
            return
        self.proc = None
        self.stopping = False
        self.append("Server stopped")
        after, self.after_exit = self.after_exit, None
        self.update_controls()
        if after:
            after()

    def stop(self):
        if self.proc is not None:
            self.stop_proc()
        else:
            self.run_steps([["stop", SERVICE]], "Stopping…")

    def stop_proc(self, then=None):
        proc = self.proc
        if proc is None:
            if then:
                then()
            return
        self.after_exit = then
        if self.stopping:
            return
        self.stopping = True
        proc.send_signal(signal.SIGINT)
        GLib.timeout_add_seconds(5, self.force_stop, proc)
        self.update_controls()

    def force_stop(self, proc):
        if self.proc is proc:
            proc.force_exit()
        return GLib.SOURCE_REMOVE

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
        if self.quiet:
            return
        if switch.get_active():
            try:
                args = build_args(self.collect())
            except ValueError as e:
                self.append(str(e))
                self.set_autostart(False)
                return
            self.stop_proc(then=lambda: self.enable_service(args))
        else:
            self.run_steps(
                [["disable", "--now", SERVICE], lambda: UNIT_PATH.unlink(missing_ok=True), ["daemon-reload"]],
                "Stopping…",
            )

    def enable_service(self, args):
        write_unit(args)
        self.run_steps(
            [["daemon-reload"], ["enable", SERVICE], ["restart", SERVICE]], "Starting…", self.follow_journal
        )

    def on_close(self, _window):
        save_config(self.collect())
        if self.proc is not None:
            self.proc.send_signal(signal.SIGINT)
        if self.follower is not None:
            self.follower.force_exit()
        return False


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        Window(self).present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
