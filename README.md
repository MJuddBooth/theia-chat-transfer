# theia-chat-transfer

Move [Eclipse Theia](https://theia-ide.org) AI chat history between machines, for example
from Linux (`/home/booth`) to macOS (`/Users/booth`).

Theia stores chats as JSON under its config dir (`~/.theia-ide` for Theia IDE, or `$THEIA_CONFIG_DIR`):

| Path | Contents |
|---|---|
| `chatSessions/` | Global store, used when no workspace is open |
| `workspace-metadata/index.json` | First root folder of a workspace → random UUID |
| `workspace-metadata/<uuid>/chatSessions/` | Chats of that workspace |

Each store has one `<sessionId>.json` per chat plus an `index.json` that the chat list is built from.
The UUIDs are random, not machine-specific; only the folder paths are. So a transfer copies the stores
and rewrites absolute paths, in `workspace-metadata/index.json` and inside the chats.

## Install

```bash
pip install .            # or: pipx install .
```

No dependencies beyond the Python standard library (3.8+). Without installing, run
`PYTHONPATH=src python3 -m theia_chat_transfer ...`.

## Usage

`list` and `export` are safe while Theia is running. For `import`, either:

- close Theia first, because a running Theia caches each `index.json` in memory and writes that copy
  back on its next autosave, dropping the imported chats from the chat list; or
- use the [extension](#importing-while-theia-is-running), which imports and then makes the running
  Theia re-read the chat list.

```bash
# source machine
theia-chat-transfer list
theia-chat-transfer export chats.tar.gz

# target machine
theia-chat-transfer import chats.tar.gz --dry-run
theia-chat-transfer import chats.tar.gz
theia-chat-transfer import chats.tar.gz --map /home/booth/git=/Users/booth/src
```

- The source home dir is mapped to the target home dir automatically (`--no-home-map` to turn off).
  `--map OLD=NEW` adds more mappings; the longest matching prefix wins. Only whole path segments
  match, and `file://` URIs (including percent-encoded ones) are rewritten too.
- Chats are merged into existing stores; when a chat exists on both sides, the later save wins.
  Each touched `index.json` is rebuilt from the chat files.
- Existing stores are backed up to `<config dir>/chat-transfer-backups/` before an import.

## Importing while Theia is running

[`vscode-extension/`](vscode-extension/README.md) is a small Theia IDE extension that runs this tool
from the command palette (**Theia Chat Transfer: Export Chats... / Import Chats... / Reload Chat List
from Disk**). After an import it makes Theia re-read the chat list, so Theia can stay open. Avoid
importing while a chat reply is streaming; the extension warns if a chat was saved in the last
10 seconds.

The extension needs this tool installed. Theia's extension host may not see your shell's `PATH`, so
set `theiaChatTransfer.cliPath` to the tool's full path, for example:

```bash
python3 -m venv ~/.local/share/theia-chat-transfer-venv
~/.local/share/theia-chat-transfer-venv/bin/pip install .
# then in Theia user settings:
# "theiaChatTransfer.cliPath": "/home/<you>/.local/share/theia-chat-transfer-venv/bin/theia-chat-transfer"
```

## Limitations

- Theia files workspace chats under the workspace's **first root folder**, not the
  `.code-workspace` file, so workspaces sharing a first folder share one chat list.
- Only chats are transferred, not other per-workspace metadata (e.g. AI memory).
- POSIX paths only (Linux, macOS). Linux → macOS path rewriting is covered by unit tests but
  hasn't yet been tried on a real Mac.

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -b -v
# or, with pytest installed:
pytest
```

## License

GNU General Public License v3.0 or later (`GPL-3.0-or-later`). See [LICENSE](LICENSE).
