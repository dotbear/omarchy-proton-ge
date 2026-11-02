# Proton-GE for Omarchy

A small Omarchy shell plugin that keeps native Steam's **Proton-GE Latest**
updated from GloriousEggroll's official prebuilt GitHub releases. Python's
standard library and Omarchy's existing QML components are the only dependencies.
Nothing is compiled.

Click the gamepad icon to check versions, update, pause/resume automatic updates,
or restore the previous version. Arrow keys/Tab select actions, Enter activates,
and Escape dismisses the popup. English and Danish strings follow Omarchy's
language setting.

The service checks on startup and hourly while the Omarchy shell is running.
Automatic updates start enabled; the popup toggle disables network polling and
automatic downloads. A lightweight local tick every minute handles deferred
work. Failed network requests retry on the next hourly check; Check now retries
immediately. Explicitly requested updates also wait for running Proton/Wine
games to exit. ProtonPlus's updater should be disabled to avoid two managers
writing the same installation.

Downloads use fixed official HTTPS endpoints, bounded requests, architecture
selection, size limits, and the GitHub asset's SHA-256 digest. Extraction rejects
escaping paths/links and limits file count and expanded size. Installation is
staged, then atomically exchanged with the current directory. One previous
installation is retained outside Steam's compatibility-tool discovery directory.
A small transaction record recovers an interrupted exchange. No downloaded code
is executed during installation. Steam is never stopped or restarted by this
plugin. The popup reports when restarting Steam is needed.

Existing `user_settings.py` is preserved. Game prefixes under `steamapps/compatdata`
are untouched. The helper maintains `Proton-GE Latest` as Steam's default and all
existing explicit game compatibility selections. If Steam is open, it queues that
change until Steam exits, to avoid Steam overwriting its configuration. Games
without explicit overrides inherit the default. The original configuration is
backed up before its first change. A manual change to another tool is converted
back to Latest after Steam exits, matching this plugin's intended policy.

User state lives in `${XDG_STATE_HOME:-~/.local/state}/dotbear.proton-ge/`.
The previous runner and installation transactions live in
`<Steam>/.omarchy-proton-ge/`. ProtonPlus-compatible `.protonplus` metadata is
preserved so the installation remains recognizable to ProtonPlus.
Restoring the previous version pauses automatic updates to prevent immediately
reinstalling the release being rolled back.

## Requirements

- Omarchy 4.5 or newer (the shell plugin system)
- Native Steam (`~/.local/share/Steam`); the Flatpak build is not supported
- Python 3 (standard library only; Omarchy ships it)

## Install

```sh
omarchy plugin add https://github.com/dotbear/omarchy-proton-ge.git --enable
```

This clones the repository into `~/.config/omarchy/plugins/dotbear.proton-ge/`,
validates it, and offers to place the gamepad widget on the bar. Restart Steam
after the first installation so it discovers **Proton-GE Latest**.

Update or remove it with Omarchy's plugin commands:

```sh
omarchy plugin update dotbear.proton-ge
omarchy plugin remove dotbear.proton-ge
```

Removing the plugin leaves the installed Proton-GE runner, its previous
version and Steam's compatibility settings in place.

## Command line

The helper the widget uses also works from a terminal. It shares the widget's
lock and state:

```sh
cd ~/.config/omarchy/plugins/dotbear.proton-ge
python proton_ge.py status
python proton_ge.py check
python proton_ge.py update
python proton_ge.py rollback
python proton_ge.py pause
python proton_ge.py resume
python proton_ge.py sync  # apply/queue Steam selections without networking
```

## Development

```sh
python -m unittest discover -s tests -v
omarchy plugin validate .
```

To try local changes, clone into the plugins folder as a real directory (not a
symlink) and run `omarchy-shell shell rescanPlugins`.

## License

MIT
