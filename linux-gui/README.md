# Audio Share GTK4 launcher

A small GTK 4 window for `as-cmd` on Linux. It builds the command line from form fields, shows the server log, and can install a systemd user service so the server starts at login.

Needs Python 3, PyGObject and GTK 4 (`python3-gi` and `gir1.2-gtk-4.0` on Debian/Ubuntu, `python3-gobject` and `gtk4` on Fedora), plus the server binary built as `as-cmd`. Press Install… and the window finds `as-cmd` (on your PATH, or in `server-core/out/install/linux-Release/bin` or `server-core/out/build/linux-Release`) and copies it to `~/.local/bin/audioshare`. It also copies the launcher to `~/.local/bin/audioshare-gui` and adds an "Audio Share Server" entry to your app menu (`~/.local/share/applications/io.github.mkckr0.AudioShareGui.desktop`, with the project icon). If it can't find `as-cmd`, it asks you to pick the file. The window runs `audioshare`, or `as-cmd` from your PATH until you install.

```sh
python3 linux-gui/audio-share-gui.py
```

- Settings are saved to `~/.config/audio-share-plus/gui.json`.
- Closing the window stops a server started from it. A systemd service keeps running.
- "Start at login" writes `~/.config/systemd/user/audio-share.service` from the current settings and enables it. Changing a setting while the server runs restarts it with the new options after a short pause.
- Uninstall… stops the server, removes the service and deletes `~/.local/bin/audioshare`, the `audioshare-gui` copy and the app menu entry. Settings in `gui.json` are kept.
- Service logs: `journalctl --user -u audio-share -f`.
