# Theia Chat Transfer (extension)

Export and import [Eclipse Theia](https://theia-ide.org) AI chat history from inside a running
Theia IDE. It's a thin front end to the `theia-chat-transfer` command line tool, which must be
installed (see the [main README](../README.md)).

## Commands

- **Theia Chat Transfer: Export Chats...** writes all chat stores to a `.tar.gz` bundle.
- **Theia Chat Transfer: Import Chats...** shows a dry run, then imports a bundle and reloads the chat list.
- **Theia Chat Transfer: Reload Chat List from Disk** makes Theia re-read its chat index files.

Each command switches the Output panel to the **Theia Chat Transfer** channel, which logs the
commands run and their output. The export and import dialogs open in the folder of the last bundle
used (your home folder the first time).

## How the reload works

Theia keeps each chat store's `index.json` in memory and writes it back on every autosave, so
chats added on disk would normally vanish from the list. The plugin API has no access to the chat
service, but Theia drops that cache whenever `ai-features.chat.sessionStorageScope` changes. The
extension flips that setting and restores it, then checks that every imported chat is in its
index, re-running the import (which rebuilds the index) up to 3 times if an autosave got in
between.

This relies on Theia internals and may break with a future Theia version. Don't import while a
chat reply is streaming; the extension warns if a chat was saved in the last 10 seconds.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `theiaChatTransfer.cliPath` | `theia-chat-transfer` | The command line tool |
| `theiaChatTransfer.configDir` | `$THEIA_CONFIG_DIR` or `~/.theia-ide` | Theia config dir |
| `theiaChatTransfer.pathMaps` | `[]` | Extra `OLD=NEW` path mappings for imports |

All three are machine-scoped, so a workspace's settings can't point the extension at another program.

## Build

```bash
npm install
npm run compile
npm run package          # creates theia-chat-transfer-<version>.vsix
```

Install the `.vsix` in Theia IDE with **Extensions: Install from VSIX...**.
