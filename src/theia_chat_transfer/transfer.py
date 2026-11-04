# SPDX-License-Identifier: GPL-3.0-or-later
"""Move Theia AI chat history between machines.

Theia keeps its chats as JSON files under its config dir (default ~/.theia-ide,
or $THEIA_CONFIG_DIR):

  chatSessions/                            global store (used when no workspace is open)
  workspace-metadata/index.json            first root folder of a workspace -> random UUID
  workspace-metadata/<uuid>/chatSessions/  store of that workspace

Every store holds one <sessionId>.json per chat plus an index.json that the chat list
is built from. The UUIDs are random and not tied to the machine; only the folder
paths are. So moving chats means rewriting absolute paths, e.g. /home/booth on Linux
to /Users/booth on macOS, both in workspace-metadata/index.json and inside the chats.

Commands:
  list              show the chat stores of this machine
  export BUNDLE     pack all chat stores into a .tar.gz bundle
  import BUNDLE     merge a bundle into this machine, rewriting paths: the home dir of
                    the source machine becomes this home dir, plus any --map OLD=NEW.
                    Chats already here are merged; the copy with the later save wins.
                    The existing stores are backed up first.

Close Theia before importing: a running Theia overwrites index.json from memory.
"""

import argparse
import io
import json
import os
import re
import socket
import tarfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from . import __version__

BUNDLE_VERSION = 1
CHATS = 'chatSessions'
WS_META = 'workspace-metadata'
INDEX = 'index.json'
MANIFEST = 'manifest.json'
GLOBAL_KEY = 'global'
SAFE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')


def default_config_dir():
    return Path(os.environ.get('THEIA_CONFIG_DIR', '~/.theia-ide')).expanduser()


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def write_json(path, data):
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False))


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    tmp.replace(path)


class PathMapper:
    """Rewrites absolute path prefixes in text, longest prefix first.

    A prefix only matches whole path segments: mapping /home/booth leaves
    /home/boothby and /mnt/home/booth alone. Matches are found in plain paths,
    in file:// URIs (also percent-encoded) and after JSON escapes such as \\n.
    """

    _BEFORE = r'(?:(?<=\\[nrt])|(?<![\w.-]))'
    _AFTER = r'(?![\w.-])'

    def __init__(self, mappings):
        self.pairs = {}
        for old, new in mappings:
            old, new = old.rstrip('/'), new.rstrip('/')
            for path in (old, new):
                if not path.startswith('/') or '"' in path or '\\' in path:
                    raise ValueError(f'Not an absolute POSIX path (or contains quotes/backslashes): {path!r}')
            if old == new:
                continue
            self.pairs[old] = new
            if quote(old) != old:
                self.pairs.setdefault(quote(old), quote(new))
        self._regex = None
        if self.pairs:
            alternatives = '|'.join(re.escape(p) for p in sorted(self.pairs, key=len, reverse=True))
            self._regex = re.compile(f'{self._BEFORE}({alternatives}){self._AFTER}')

    def rewrite(self, text):
        if self._regex is None:
            return text
        return self._regex.sub(lambda m: self.pairs[m.group(1)], text)


def session_ids(store_dir):
    if not store_dir.is_dir():
        return set()
    return {f.stem for f in store_dir.glob('*.json') if f.name != INDEX and SAFE_ID.match(f.stem)}


def find_stores(config_dir):
    """Returns (key, workspace path or None, store dir) for every chat store."""
    stores = []
    if (config_dir / CHATS).is_dir():
        stores.append((GLOBAL_KEY, None, config_dir / CHATS))
    ws_index = read_json(config_dir / WS_META / INDEX, {}) or {}
    for ws_path, ws_uuid in sorted(ws_index.items()):
        store_dir = config_dir / WS_META / ws_uuid / CHATS
        if SAFE_ID.match(ws_uuid) and ws_uuid != GLOBAL_KEY and store_dir.is_dir():
            stores.append((ws_uuid, ws_path, store_dir))
    return stores


def is_valid_entry(entry):
    """Mirrors ChatSessionStoreImpl.isValidMetadata: Theia drops other entries."""
    return (isinstance(entry, dict)
            and isinstance(entry.get('sessionId'), str)
            and isinstance(entry.get('title'), str)
            and isinstance(entry.get('location'), str)
            and isinstance(entry.get('saveDate'), (int, float))
            and entry['saveDate'] > 0)


def entry_from_session(session_id, data):
    model = data.get('model') or {}
    save_date = data.get('saveDate') or int(time.time() * 1000)
    entry = {
        'sessionId': session_id,
        'location': model.get('location') or 'panel',
        'title': data.get('title') or 'Untitled chat',
        'saveDate': save_date,
        'lastInteraction': save_date,
    }
    for key in ('pinnedAgentId', 'rootSessionId', 'parentSessionId'):
        if data.get(key):
            entry[key] = data[key]
    return entry


def export_bundle(config_dir, output, home):
    stores = find_stores(config_dir)
    if not stores:
        raise SystemExit(f'No chat stores found in {config_dir}')
    manifest = {
        'version': BUNDLE_VERSION,
        'created': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'source_host': socket.gethostname(),
        'source_home': str(home),
        'source_config_dir': str(config_dir),
        'stores': [{'key': key, 'workspace': ws} for key, ws, _ in stores],
    }
    with tarfile.open(output, 'w:gz') as tar:
        for key, ws, store_dir in stores:
            ids = sorted(session_ids(store_dir))
            for sid in ids:
                tar.add(store_dir / f'{sid}.json', arcname=f'stores/{key}/{sid}.json')
            if (store_dir / INDEX).is_file():
                tar.add(store_dir / INDEX, arcname=f'stores/{key}/{INDEX}')
            print(f'{ws or "(global)"}: {len(ids)} chats')
        data = json.dumps(manifest, indent=2).encode('utf-8')
        info = tarfile.TarInfo(MANIFEST)
        info.size = len(data)
        info.mtime = int(time.time())
        tar.addfile(info, fileobj=io.BytesIO(data))
    print(f'Wrote {output}')


def read_member(tar, name):
    try:
        member = tar.getmember(name)
    except KeyError:
        return None
    if not member.isfile():
        return None
    return tar.extractfile(member).read().decode('utf-8')


def bundle_sessions(tar):
    """Maps store key -> {session id: tar member}, ignoring anything unexpected."""
    sessions = {}
    for member in tar.getmembers():
        parts = member.name.split('/')
        if (member.isfile() and len(parts) == 3 and parts[0] == 'stores' and SAFE_ID.match(parts[1])
                and parts[2].endswith('.json') and parts[2] != INDEX and SAFE_ID.match(parts[2][:-5])):
            sessions.setdefault(parts[1], {})[parts[2][:-5]] = member
    return sessions


def backup(config_dir):
    dest = config_dir / 'chat-transfer-backups' / f'backup-{time.strftime("%Y%m%d-%H%M%S")}.tar.gz'
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, 'w:gz') as tar:
        for name in (CHATS, WS_META):
            if (config_dir / name).exists():
                tar.add(config_dir / name, arcname=name)
    return dest


def merge_store(tar, members, src_index, target_dir, mapper, dry_run):
    dst_index = read_json(target_dir / INDEX, {}) or {}
    present = session_ids(target_dir)
    imported = kept = 0
    for sid, member in sorted(members.items()):
        text = mapper.rewrite(tar.extractfile(member).read().decode('utf-8'))
        data = json.loads(text)
        existing = read_json(target_dir / f'{sid}.json') if sid in present else None
        if existing and (existing.get('saveDate') or 0) >= (data.get('saveDate') or 0):
            kept += 1
            continue
        if not dry_run:
            write_text(target_dir / f'{sid}.json', text)
        entry = src_index.get(sid)
        dst_index[sid] = entry if is_valid_entry(entry) and entry['sessionId'] == sid else entry_from_session(sid, data)
        present.add(sid)
        imported += 1

    # Rebuild the index from the chat files: add chats it is missing, drop entries without a file.
    repaired = 0
    for sid in sorted(present - dst_index.keys()):
        data = read_json(target_dir / f'{sid}.json') if (target_dir / f'{sid}.json').is_file() else None
        if data is not None:
            dst_index[sid] = entry_from_session(sid, data)
            repaired += 1
    dangling = [sid for sid in dst_index if sid not in present]
    for sid in dangling:
        del dst_index[sid]
    if not dry_run:
        write_json(target_dir / INDEX, dst_index)
    return imported, kept, repaired, len(dangling)


def import_bundle(bundle, config_dir, home, extra_maps=(), home_map=True, dry_run=False):
    with tarfile.open(bundle, 'r:gz') as tar:
        manifest_text = read_member(tar, MANIFEST)
        if manifest_text is None:
            raise SystemExit(f'{bundle} is not a chat bundle (no {MANIFEST})')
        manifest = json.loads(manifest_text)
        if manifest.get('version') != BUNDLE_VERSION:
            raise SystemExit(f'Unsupported bundle version {manifest.get("version")}')

        mappings = []
        if home_map and manifest['source_home'] != str(home):
            mappings.append((manifest['source_home'], str(home)))
        mappings.extend(extra_maps)
        mapper = PathMapper(mappings)
        for old, new in mapper.pairs.items():
            if '%' not in old:
                print(f'Mapping {old} -> {new}')

        sessions = bundle_sessions(tar)
        ws_index_file = config_dir / WS_META / INDEX
        ws_index = read_json(ws_index_file, {}) or {}
        ws_index_changed = False
        prefix = '[dry run] ' if dry_run else ''
        if not dry_run:
            print(f'Backed up existing stores to {backup(config_dir)}')

        for store in manifest['stores']:
            key, ws = store['key'], store['workspace']
            if not SAFE_ID.match(key):
                raise ValueError(f'Unsafe store key in bundle: {key!r}')
            if ws is None:
                target_dir, label = config_dir / CHATS, '(global)'
            else:
                new_ws = mapper.rewrite(ws)
                target_uuid = ws_index.get(new_ws)
                if target_uuid is None:
                    target_uuid = key if key not in ws_index.values() else str(uuid.uuid4())
                    ws_index[new_ws] = target_uuid
                    ws_index_changed = True
                elif not SAFE_ID.match(target_uuid):
                    raise ValueError(f'Unexpected UUID {target_uuid!r} in {ws_index_file}')
                target_dir = config_dir / WS_META / target_uuid / CHATS
                label = ws if new_ws == ws else f'{ws} -> {new_ws}'
                if not Path(new_ws).is_dir():
                    label += '  [folder does not exist here; Theia finds the chats once it does]'

            src_index_text = read_member(tar, f'stores/{key}/{INDEX}')
            src_index = json.loads(src_index_text) if src_index_text else {}
            imported, kept, repaired, dropped = merge_store(
                tar, sessions.get(key, {}), src_index, target_dir, mapper, dry_run)
            print(f'{prefix}{label}\n    {imported} imported, {kept} kept (same or newer here)'
                  + (f', {repaired} added to index' if repaired else '')
                  + (f', {dropped} index entries without a file dropped' if dropped else ''))

        if ws_index_changed and not dry_run:
            write_json(ws_index_file, ws_index)


def list_stores(config_dir):
    stores = find_stores(config_dir)
    if not stores:
        print(f'No chat stores found in {config_dir}')
    for _, ws, store_dir in stores:
        index = read_json(store_dir / INDEX, {}) or {}
        label = ws or '(global: chats from windows without a workspace)'
        if ws is not None and not Path(ws).is_dir():
            label += '  [folder missing on this machine]'
        print(f'{label}\n    {store_dir}\n    {len(session_ids(store_dir))} chat files, {len(index)} in index')


def parse_map(value):
    old, sep, new = value.partition('=')
    if not sep or not old or not new:
        raise argparse.ArgumentTypeError(f'expected OLD=NEW, got {value!r}')
    return old, new


def main(argv=None):
    parser = argparse.ArgumentParser(prog='theia-chat-transfer', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('--config-dir', type=Path, default=default_config_dir(),
                        help='Theia config dir (default: $THEIA_CONFIG_DIR or ~/.theia-ide)')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list', help='show the chat stores of this machine')
    export = sub.add_parser('export', help='pack all chat stores into a bundle')
    export.add_argument('bundle', type=Path)
    imp = sub.add_parser('import', help='merge a bundle into this machine')
    imp.add_argument('bundle', type=Path)
    imp.add_argument('--map', type=parse_map, action='append', default=[], metavar='OLD=NEW',
                     help='rewrite an absolute path prefix; repeatable, longest prefix wins')
    imp.add_argument('--no-home-map', action='store_true',
                     help='do not rewrite the home dir of the source machine to this one')
    imp.add_argument('--dry-run', action='store_true', help='report what would happen without writing')
    args = parser.parse_args(argv)

    config_dir = args.config_dir.expanduser()
    if args.command == 'list':
        list_stores(config_dir)
    elif args.command == 'export':
        export_bundle(config_dir, args.bundle, Path.home())
    else:
        print('Make sure Theia is closed, or it will overwrite the chat lists from memory.')
        try:
            import_bundle(args.bundle, config_dir, Path.home(), args.map, not args.no_home_map, args.dry_run)
        except ValueError as e:
            parser.error(str(e))
    return 0
