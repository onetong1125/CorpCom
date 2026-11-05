"""Exercise workspace file tools through their model-facing interface."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from corpcom.workspace import WorkspaceStore
from corpcom.workspace_tools import create_workspace_tools


class WorkspaceFileToolTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(TemporaryDirectory())
        self.data = Path(temporary).resolve() / 'data'
        with WorkspaceStore(self.data) as store:
            self.wid = store.create_workspace('文件工具').id
            self.other = store.create_workspace('另一个工作区').id
        self.root = self.data / 'workspaces' / self.wid
        self.tools = {t.name: t for t in create_workspace_tools(self.data, self.wid)}

    def error(self, name, args):
        result = self.tools[name].invoke({
            'type': 'tool_call', 'id': 'file-operation', 'name': name, 'args': args,
        })
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.tool_call_id, 'file-operation')
        self.assertNotIn(str(self.data), result.content)
        return result.content

    def test_schema_and_write_create_overwrite_without_registration(self):
        write_schema = self.tools['write_file'].tool_call_schema.model_json_schema()
        edit_schema = self.tools['edit_file'].tool_call_schema.model_json_schema()
        self.assertEqual(set(write_schema['properties']), {'file_path', 'content'})
        self.assertEqual(set(edit_schema['properties']), {'file_path', 'old_string', 'new_string', 'replace_all'})
        self.assertFalse(edit_schema['properties']['replace_all']['default'])
        for content in ['中文\n', '覆盖', '']:
            result = self.tools['write_file'].invoke({'file_path': 'tasks/demo/note.txt', 'content': content})
            self.assertEqual(result, {'status': 'ok', 'file_path': 'tasks/demo/note.txt'})
            self.assertEqual((self.root / 'tasks/demo/note.txt').read_text(), content)
        with WorkspaceStore(self.data) as store:
            self.assertEqual(store.get_workspace_detail(self.wid).artifacts, [])

    def test_edit_unique_all_and_delete(self):
        path = self.root / 'note.txt'
        path.write_text('甲 乙 甲', encoding='utf-8')
        self.tools['edit_file'].invoke({'file_path': 'note.txt', 'old_string': '乙', 'new_string': '丙'})
        self.assertEqual(path.read_text(), '甲 丙 甲')
        result = self.tools['edit_file'].invoke({'file_path': 'note.txt', 'old_string': '甲', 'new_string': '', 'replace_all': True})
        self.assertEqual(result['replacements'], 2)
        self.assertEqual(path.read_text(), ' 丙 ')

    def test_failed_edits_leave_original_file_unchanged(self):
        path = self.root / 'note.txt'
        path.write_text('same same')
        for old, replace_all in [('', False), ('missing', False), ('missing', True), ('same', False)]:
            with self.subTest(old=old, replace_all=replace_all):
                self.error('edit_file', {'file_path': 'note.txt', 'old_string': old, 'new_string': 'new', 'replace_all': replace_all})
                self.assertEqual(path.read_text(), 'same same')
        self.assertIn('not found', self.error('edit_file', {'file_path': 'missing.txt', 'old_string': 'a', 'new_string': 'b'}))
        path.write_bytes(b'\xff')
        self.assertIn('UTF-8', self.error('edit_file', {'file_path': 'note.txt', 'old_string': 'a', 'new_string': 'b'}))
        self.assertEqual(path.read_bytes(), b'\xff')

    def test_paths_cannot_escape_workspace(self):
        other_root = self.data / 'workspaces' / self.other
        target = other_root / 'private.txt'
        target.write_text('private')
        (self.root / 'outside').symlink_to(other_root, target_is_directory=True)
        (self.root / 'folder').mkdir()
        for value in ['', '.', '/workspace/note.txt', str(target), f'../{self.other}/private.txt', 'outside/private.txt', 'folder']:
            for name, args in [('write_file', {'content': 'changed'}), ('edit_file', {'old_string': 'private', 'new_string': 'changed'})]:
                with self.subTest(path=value, tool=name):
                    self.error(name, {'file_path': value, **args})
        self.assertEqual(target.read_text(), 'private')

    def test_two_tool_instances_serialize_edits_through_symlink_alias(self):
        path = self.root / 'note.txt'
        path.write_text('left right')
        (self.root / 'alias.txt').symlink_to(path)
        other_tools = {t.name: t for t in create_workspace_tools(self.data, self.wid)}
        first_read, release, second_started, second_read = Event(), Event(), Event(), Event()
        original_read = Path.read_text
        count = 0

        def held_read(current, *args, **kwargs):
            nonlocal count
            if current == path:
                count += 1
                if count == 1:
                    first_read.set()
                    if not release.wait(5):
                        raise AssertionError('test did not release first read')
                else:
                    second_read.set()
            return original_read(current, *args, **kwargs)

        def second_edit():
            second_started.set()
            return other_tools['edit_file'].invoke({'file_path': 'alias.txt', 'old_string': 'right', 'new_string': 'R'})

        with patch.object(Path, 'read_text', held_read), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.tools['edit_file'].invoke, {'file_path': 'note.txt', 'old_string': 'left', 'new_string': 'L'})
            try:
                self.assertTrue(first_read.wait(5))
                second = pool.submit(second_edit)
                self.assertTrue(second_started.wait(5))
                self.assertFalse(second_read.wait(0.2), 'second edit read before first edit released its lock')
            finally:
                release.set()
            self.assertEqual(first.result()['status'], 'ok')
            self.assertEqual(second.result()['status'], 'ok')
        self.assertEqual(path.read_text(), 'L R')

    def test_write_waits_for_edit_lock(self):
        path = self.root / 'note.txt'
        path.write_text('original')
        first_read, release, writer_started, writer_entered = Event(), Event(), Event(), Event()
        original_read, original_write = Path.read_text, Path.write_text

        def held_read(current, *args, **kwargs):
            if current == path:
                first_read.set()
                if not release.wait(5):
                    raise AssertionError('test did not release read')
            return original_read(current, *args, **kwargs)

        def observe_write(current, content, *args, **kwargs):
            if current == path and content == 'overwrite':
                writer_entered.set()
            return original_write(current, content, *args, **kwargs)

        def writer():
            writer_started.set()
            return self.tools['write_file'].invoke({'file_path': 'note.txt', 'content': 'overwrite'})

        with patch.object(Path, 'read_text', held_read), patch.object(Path, 'write_text', observe_write), ThreadPoolExecutor(max_workers=2) as pool:
            edit = pool.submit(self.tools['edit_file'].invoke, {'file_path': 'note.txt', 'old_string': 'original', 'new_string': 'edited'})
            try:
                self.assertTrue(first_read.wait(5))
                write = pool.submit(writer)
                self.assertTrue(writer_started.wait(5))
                self.assertFalse(writer_entered.wait(0.2), 'write bypassed the edit lock')
            finally:
                release.set()
            self.assertEqual(edit.result()['status'], 'ok')
            self.assertEqual(write.result()['status'], 'ok')
        self.assertEqual(path.read_text(), 'overwrite')

    def test_unexpected_io_errors_propagate(self):
        with patch.object(Path, 'write_text', side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(OSError, 'disk failure'):
                self.tools['write_file'].invoke({'file_path': 'note.txt', 'content': 'text'})


if __name__ == '__main__':
    unittest.main()
