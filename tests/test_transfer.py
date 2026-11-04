# SPDX-License-Identifier: GPL-3.0-or-later
import io
import json
import shutil
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from theia_chat_transfer import transfer as t


def session(sid, title, save_date, text):
    return {
        'version': 1, 'title': title, 'pinnedAgentId': 'Coder', 'saveDate': save_date,
        'model': {'sessionId': sid, 'location': 'panel', 'requests': [{'id': 'r1', 'text': text}], 'responses': []},
    }


def entry(sid, title, save_date):
    return {'sessionId': sid, 'location': 'panel', 'title': title, 'pinnedAgentId': 'Coder', 'saveDate': save_date}


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def read(path):
    return json.loads(path.read_text())


class PathMapperTest(unittest.TestCase):

    def setUp(self):
        self.mapper = t.PathMapper([('/home/booth', '/Users/booth'), ('/home/booth/git/mddb', '/Users/booth/src/mddb')])

    def test_rewrites_uris_and_plain_paths(self):
        self.assertEqual(self.mapper.rewrite('file:///home/booth/x.json and /home/booth'),
                         'file:///Users/booth/x.json and /Users/booth')

    def test_longest_prefix_wins(self):
        self.assertEqual(self.mapper.rewrite('/home/booth/git/mddb/a.py'), '/Users/booth/src/mddb/a.py')
        self.assertEqual(self.mapper.rewrite('/home/booth/git/mddb2'), '/Users/booth/git/mddb2')

    def test_only_matches_whole_segments(self):
        for text in ('/home/boothby/x', '/mnt/home/booth/x', '/home/booth.bak'):
            self.assertEqual(self.mapper.rewrite(text), text)

    def test_matches_after_json_escapes(self):
        self.assertEqual(self.mapper.rewrite(r'listing:\n/home/booth/git\\n/home/booth'),
                         r'listing:\n/Users/booth/git\\n/Users/booth')
        self.assertEqual(self.mapper.rewrite(r'{\"path\":\"/home/booth/a\"}'), r'{\"path\":\"/Users/booth/a\"}')

    def test_percent_encoded_uris(self):
        mapper = t.PathMapper([('/home/booth/My Docs', '/Users/booth/Docs')])
        self.assertEqual(mapper.rewrite('file:///home/booth/My%20Docs/a and /home/booth/My Docs/b'),
                         'file:///Users/booth/Docs/a and /Users/booth/Docs/b')

    def test_rejects_bad_paths(self):
        for bad in [('home', '/x'), ('/x', 'C:\\x'), ('/x"', '/y')]:
            self.assertRaises(ValueError, t.PathMapper, [bad])


class TransferTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.src = self.tmp / 'src'
        self.dst = self.tmp / 'dst'
        self.bundle = self.tmp / 'chats.tar.gz'

        write(self.src / 'workspace-metadata/index.json', {'/home/booth/git/mddb': 'uuid-src'})
        store = self.src / 'workspace-metadata/uuid-src/chatSessions'
        write(store / 's1.json', session('s1', 'First', 1000, 'open file:///home/booth/git/mddb/a.py'))
        write(store / 's3.json', session('s3', 'Third', 3000, 'third'))
        write(store / 'index.json', {'s1': entry('s1', 'First', 1000), 's3': entry('s3', 'Third', 3000)})
        write(self.src / 'chatSessions/g1.json', session('g1', 'Global', 500, '/home/booth/notes'))
        write(self.src / 'chatSessions/index.json', {})
        with redirect_stdout(io.StringIO()):
            t.export_bundle(self.src, self.bundle, home='/home/booth')

    def do_import(self, **kwargs):
        with redirect_stdout(io.StringIO()):
            t.import_bundle(self.bundle, self.dst, home='/Users/booth', **kwargs)

    def test_import_into_empty_machine(self):
        self.do_import()
        self.assertEqual(read(self.dst / 'workspace-metadata/index.json'), {'/Users/booth/git/mddb': 'uuid-src'})
        store = self.dst / 'workspace-metadata/uuid-src/chatSessions'
        s1 = (store / 's1.json').read_text()
        self.assertIn('file:///Users/booth/git/mddb/a.py', s1)
        self.assertNotIn('/home/booth', s1)
        self.assertEqual(read(store / 'index.json')['s1'], entry('s1', 'First', 1000))
        global_index = read(self.dst / 'chatSessions/index.json')
        self.assertEqual(global_index['g1']['title'], 'Global')
        self.assertTrue(t.is_valid_entry(global_index['g1']))
        self.assertIn('/Users/booth/notes', (self.dst / 'chatSessions/g1.json').read_text())
        self.assertEqual(len(list((self.dst / 'chat-transfer-backups').glob('*.tar.gz'))), 1)

    def test_merges_into_existing_workspace_store(self):
        write(self.dst / 'workspace-metadata/index.json', {'/Users/booth/git/mddb': 'uuid-dst'})
        store = self.dst / 'workspace-metadata/uuid-dst/chatSessions'
        write(store / 's1.json', session('s1', 'Newer here', 2000, 'x'))
        write(store / 's3.json', session('s3', 'Older here', 100, 'x'))
        write(store / 'orphan.json', session('orphan', 'Not in index', 50, 'x'))
        write(store / 'index.json', {'s1': entry('s1', 'Newer here', 2000), 's3': entry('s3', 'Older here', 100),
                                     'gone': entry('gone', 'No file', 10)})
        self.do_import()

        self.assertEqual(read(self.dst / 'workspace-metadata/index.json'), {'/Users/booth/git/mddb': 'uuid-dst'})
        self.assertFalse((self.dst / 'workspace-metadata/uuid-src').exists())
        index = read(store / 'index.json')
        self.assertEqual(sorted(index), ['orphan', 's1', 's3'])
        self.assertEqual(index['s1']['title'], 'Newer here')
        self.assertEqual(read(store / 's1.json')['title'], 'Newer here')
        self.assertEqual(index['s3']['title'], 'Third')
        self.assertEqual(read(store / 's3.json')['title'], 'Third')
        self.assertEqual(index['orphan']['title'], 'Not in index')

    def test_dry_run_writes_nothing(self):
        self.do_import(dry_run=True)
        self.assertFalse(self.dst.exists())

    def test_explicit_maps_without_home_map(self):
        self.do_import(home_map=False, extra_maps=[('/home/booth/git', '/srv/git')])
        self.assertEqual(read(self.dst / 'workspace-metadata/index.json'), {'/srv/git/mddb': 'uuid-src'})
        self.assertIn('/home/booth/notes', (self.dst / 'chatSessions/g1.json').read_text())

    def test_rejects_unsafe_store_key(self):
        evil = self.tmp / 'evil.tar.gz'
        manifest = json.dumps({'version': 1, 'source_home': '/home/booth',
                               'stores': [{'key': '..', 'workspace': '/home/booth/x'}]}).encode()
        with tarfile.open(evil, 'w:gz') as tar:
            info = tarfile.TarInfo('manifest.json')
            info.size = len(manifest)
            tar.addfile(info, io.BytesIO(manifest))
        with redirect_stdout(io.StringIO()):
            self.assertRaises(ValueError, t.import_bundle, evil, self.dst, '/Users/booth')


class CliTest(unittest.TestCase):

    def test_list_on_empty_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(t.main(['--config-dir', tmp, 'list']), 0)
        self.assertIn('No chat stores found', out.getvalue())

    def test_rejects_malformed_map(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            t.main(['import', 'x.tar.gz', '--map', 'no-equals-sign'])


if __name__ == '__main__':
    unittest.main()
