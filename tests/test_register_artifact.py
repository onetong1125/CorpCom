"""Verify explicit artifact registration through storage, tools, and HTTP reads."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from corpcom.api import create_app
from corpcom.workspace import WorkspaceStore
from corpcom.workspace_tools import create_workspace_tools


class RegisterArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(TemporaryDirectory())
        self.data = Path(temporary).resolve() / 'data'
        with WorkspaceStore(self.data) as store:
            self.wid = store.create_workspace('登记测试').id
            self.other = store.create_workspace('其他研究').id
        self.root = self.data / 'workspaces' / self.wid
        self.tools = {t.name: t for t in create_workspace_tools(self.data, self.wid)}
        self.tools['write_file'].invoke({'file_path': 'tasks/demo/report.md', 'content': '初稿'})
        self.path = self.root / 'tasks/demo/report.md'

    def test_tool_schema_and_registration_without_copy(self):
        schema = self.tools['register_artifact'].tool_call_schema.model_json_schema()
        self.assertEqual(set(schema['properties']), {'file_path', 'kind'})
        before = set(self.root.rglob('*'))
        result = self.tools['register_artifact'].invoke({'file_path': 'tasks/demo/report.md', 'kind': 'report'})
        self.assertEqual(set(result), {'artifact_id', 'name', 'kind', 'relative_path'})
        self.assertEqual(set(self.root.rglob('*')), before)
        self.assertEqual(result['relative_path'], self.path.relative_to(self.data).as_posix())
        with WorkspaceStore(self.data) as store:
            self.assertEqual(store.get_file(self.wid, result['artifact_id']).path, self.path)
            self.assertEqual(len(store.get_workspace_detail(self.wid).artifacts), 1)

    def test_repeat_and_alias_keep_id_and_kind(self):
        (self.root / 'alias.md').symlink_to(self.path)
        first = self.tools['register_artifact'].invoke({'file_path': 'tasks/demo/report.md', 'kind': 'report'})
        second = self.tools['register_artifact'].invoke({'file_path': 'alias.md', 'kind': 'data'})
        self.assertEqual(second, first)

    def test_invalid_paths_and_kind_do_not_register(self):
        other_file = self.data / 'workspaces' / self.other / 'private.txt'
        other_file.write_text('private')
        (self.root / 'outside').symlink_to(other_file)
        for path, kind in [('', 'report'), ('.', 'report'), ('tasks', 'report'), ('missing.txt', 'report'),
                           ('/workspace/tasks/demo/report.md', 'report'), (str(self.path), 'report'),
                           (f'../{self.other}/private.txt', 'report'), ('outside', 'report'),
                           ('tasks/demo/report.md', '  ')]:
            with self.subTest(path=path, kind=kind):
                result = self.tools['register_artifact'].invoke({
                    'type': 'tool_call', 'id': 'register', 'name': 'register_artifact',
                    'args': {'file_path': path, 'kind': kind},
                })
                self.assertEqual(result.status, 'error')
                self.assertEqual(result.tool_call_id, 'register')
                self.assertNotIn(str(self.data), result.content)
        with WorkspaceStore(self.data) as store:
            self.assertEqual(store.get_workspace_detail(self.wid).artifacts, [])
        self.assertEqual(other_file.read_text(), 'private')

    def test_existing_import_and_python_archive_are_reused(self):
        with WorkspaceStore(self.data) as store:
            imported = store.import_file(self.wid, self.path, 'report')
            run = self.root / '.runs' / 'example'
            run.mkdir(parents=True)
            (run / 'result.txt').write_text('calculation')
            archived = store.archive_run_files(self.wid, run, [(Path('result.txt'), 'data')], archive_path=Path('calc/v1'))[0]
            for artifact in [imported, archived]:
                actual = store.register_workspace_file(self.wid, artifact.path.relative_to(self.root), artifact.kind)
                self.assertEqual(actual.id, artifact.id)
            self.assertEqual(len(store.get_workspace_detail(self.wid).artifacts), 2)

    def test_concurrent_connections_create_one_record(self):
        ready = Barrier(4)

        def register():
            with WorkspaceStore(self.data) as store:
                ready.wait(timeout=5)
                return store.register_workspace_file(self.wid, Path('tasks/demo/report.md'), 'report').id

        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda _: register(), range(4)))
        self.assertEqual(len(set(ids)), 1)
        with WorkspaceStore(self.data) as store:
            self.assertEqual(len(store.get_workspace_detail(self.wid).artifacts), 1)

    def test_frontend_endpoints_read_updated_content(self):
        result = self.tools['register_artifact'].invoke({'file_path': 'tasks/demo/report.md', 'kind': 'report'})
        artifact_id = result['artifact_id']
        with TestClient(create_app(self.data)) as client:
            url = f'/api/workspaces/{self.wid}'
            listed = client.get(url).json()['artifacts']
            self.assertEqual([a['id'] for a in listed], [artifact_id])
            self.assertEqual(client.get(f'{url}/files/{artifact_id}').json()['kind'], 'report')
            download = client.get(f'{url}/files/{artifact_id}/content')
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.content.decode(), '初稿')
            self.tools['edit_file'].invoke({'file_path': 'tasks/demo/report.md', 'old_string': '初稿', 'new_string': '修订'})
            self.assertEqual(client.get(f'{url}/files/{artifact_id}/content').content.decode(), '修订')
            self.assertEqual(client.get(f'/api/workspaces/{self.other}/files/{artifact_id}/content').status_code, 404)

    def test_database_failure_is_not_swallowed(self):
        with patch.object(WorkspaceStore, 'register_workspace_file', side_effect=sqlite3.OperationalError('database failure')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'database failure'):
                self.tools['register_artifact'].invoke({'file_path': 'tasks/demo/report.md', 'kind': 'report'})


if __name__ == '__main__':
    unittest.main()
