import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _resolve(value, created):
    if isinstance(value, str):
        for key, item in created.items():
            value = value.replace("{" + key + "}", str(item))
        return value
    if isinstance(value, list):
        return [_resolve(item, created) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, created) for key, item in value.items()}
    return value


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_workflow(self):
        created = {}
        steps = [{'op': 'create', 'as': 'participant', 'kind': 'participant', 'data': {'name': 'Participant One'}}, {'op': 'create', 'as': 'consent', 'kind': 'consent', 'data': {'participant_id': '{participant}', 'scope': ['research']}}, {'op': 'transition', 'target': 'consent', 'action': 'activate', 'data': {'scope': ['research'], 'version': 'v1', 'expires_at': '2099-01-01'}, 'expect': 'active'}, {'op': 'create', 'as': 'sample', 'kind': 'sample', 'data': {'participant_id': '{participant}', 'sample_code': 'B-001', 'collected_at': '2026-01-01'}}, {'op': 'transition', 'target': 'sample', 'action': 'store', 'data': {'freezer': 'F1', 'position': 'A1', 'consent_id': '{consent}'}, 'expect': 'stored'}, {'op': 'create', 'as': 'withdrawal', 'kind': 'withdrawal', 'data': {'participant_id': '{participant}', 'requested_at': '2026-03-01'}}, {'op': 'transition', 'target': 'withdrawal', 'action': 'approve', 'data': {'reason': 'participant request', 'sample_ids': ['{sample}']}, 'expect': 'approved'}, {'op': 'transition', 'target': 'withdrawal', 'action': 'execute', 'data': {'executed_at': '2026-03-02'}, 'expect': 'executed'}]
        for step in steps:
            if step["op"] == "create":
                entity = self.service.create(
                    self.actor,
                    step["kind"],
                    _resolve(step.get("data", {}), created),
                    step.get("idempotency_key"),
                )
                created[step["as"]] = entity["id"]
            else:
                entity = self.service.transition(
                    self.actor,
                    created[step["target"]],
                    step["action"],
                    _resolve(step.get("data", {}), created),
                    step.get("expected_version"),
                )
            if "expect" in step:
                self.assertEqual(entity["status"], step["expect"])


if __name__ == "__main__":
    unittest.main()
