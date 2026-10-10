"""Verify typed storage results and their serialization after reopening a database."""

from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from corpcom.workspace import WorkspaceStore
from corpcom.workspace_models import (
    Artifact, ArtifactFile, Message, Thread, ThreadDetail,
    Workspace, WorkspaceDetail, WorkspaceSummary,
)


class WorkspaceModelTests(unittest.TestCase):
    def test_reopened_store_returns_typed_records_and_detail_views(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "plan.md"
            source.write_text("Research plan", encoding="utf-8")
            with WorkspaceStore(root / "data") as store:
                workspace = store.create_workspace(
                    title="研究", goal="检验假设", scope="单家公司", observation_period="三个月",
                    initial_hypothesis="待验证", preferences={"关注": ["现金流"], "风险偏好": None},
                )
                self.assertIs(type(workspace), Workspace)
                thread = store.create_thread(workspace.id, "初始讨论")
                self.assertIs(type(thread), Thread)
                message = store.add_message(workspace.id, thread.id, "user", "关注现金流")
                self.assertIsInstance(message, Message)
                artifact = store.import_file(workspace.id, source, "plan")
                self.assertIsInstance(artifact, ArtifactFile)
                store.set_preferences(workspace.id, {"关注": ["现金流"], "报告语言": "中文"})

            with WorkspaceStore(root / "data") as store:
                # Basic metadata stays independent of potentially large related collections.
                restored = store.get_workspace(workspace.id)
                self.assertIs(type(restored), Workspace)
                self.assertNotIn("threads", restored.model_dump())
                self.assertEqual(restored.preferences["报告语言"], "中文")
                self.assertIsInstance(restored.created_at, datetime)
                self.assertIsNotNone(restored.created_at.tzinfo)
                self.assertIsInstance(store.list_workspaces()[0], WorkspaceSummary)

                detail = store.get_workspace_detail(workspace.id)
                self.assertIsInstance(detail, WorkspaceDetail)
                self.assertIsInstance(detail.threads[0], Thread)
                self.assertIsInstance(detail.artifacts[0], Artifact)
                history = store.get_thread(workspace.id, thread.id)
                self.assertIsInstance(history, ThreadDetail)
                self.assertIsInstance(history.messages[0], Message)
                self.assertEqual(history.messages[0].content, "关注现金流")
                file = store.get_file(workspace.id, artifact.id)
                self.assertIsInstance(file.path, Path)
                self.assertEqual(file.path.read_text(), "Research plan")

                # Nested records, datetime values and Paths must all serialize to valid JSON.
                payload = json.loads(json.dumps(detail.model_dump(mode="json"), allow_nan=False))
                self.assertEqual(payload["threads"][0]["id"], thread.id)
                self.assertEqual(payload["artifacts"][0]["relative_path"], artifact.relative_path)
                self.assertRegex(payload["created_at"], r"T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
                self.assertEqual(file.model_dump(mode="json")["path"], str(file.path))


if __name__ == "__main__":
    unittest.main()
