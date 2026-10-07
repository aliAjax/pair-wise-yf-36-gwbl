import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from src.domain import Actor, ConflictError, ValidationError
from src.http_api import create_server
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


class ConsentScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _participant(self, name="Participant"):
        return self.service.create(self.actor, "participant", {"name": name})

    def _consent(self, participant_id, scope, version):
        return self.service.create(
            self.actor, "consent", {"participant_id": participant_id, "scope": scope}
        )

    def _activate(self, consent_id, scope, version):
        return self.service.transition(
            self.actor,
            consent_id,
            "activate",
            {"scope": scope, "version": version, "expires_at": "2099-01-01"},
        )

    def _store_sample(self, participant_id, consent_id, code):
        sample = self.service.create(
            self.actor,
            "sample",
            {
                "participant_id": participant_id,
                "sample_code": code,
                "collected_at": "2026-01-01",
            },
        )
        self.service.transition(
            self.actor,
            sample["id"],
            "store",
            {"freezer": "F1", "position": "A1", "consent_id": consent_id},
        )
        return sample

    def test_new_consent_restricts_uncovered_uses_and_blocks_operations(self):
        participant = self._participant()
        consent_v1 = self._consent(participant["id"], ["research"], "v1")
        self._activate(consent_v1["id"], ["research"], "v1")
        sample = self._store_sample(participant["id"], consent_v1["id"], "B-001")

        consent_v2 = self._consent(participant["id"], ["clinical"], "v2")
        self._activate(consent_v2["id"], ["clinical"], "v2")

        stored = self.service.get(sample["id"])
        self.assertTrue(stored["data"]["restricted"])
        self.assertEqual(stored["data"]["restricted_uses"], ["research"])
        self.assertEqual(stored["data"]["restricted_by_consent_version"], "v2")

        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor,
                sample["id"],
                "loan",
                {"recipient": "lab", "purpose": "research", "due_at": "2026-12-31"},
            )
        with self.assertRaises(ValidationError):
            self.service.transition(self.actor, sample["id"], "anonymize", {"reason": "study"})

    def test_resigning_new_consent_lifts_restriction(self):
        participant = self._participant()
        consent_v1 = self._consent(participant["id"], ["research"], "v1")
        self._activate(consent_v1["id"], ["research"], "v1")
        sample = self._store_sample(participant["id"], consent_v1["id"], "B-001")

        consent_v2 = self._consent(participant["id"], ["clinical"], "v2")
        self._activate(consent_v2["id"], ["clinical"], "v2")
        self.assertTrue(self.service.get(sample["id"])["data"]["restricted"])

        consent_v3 = self._consent(participant["id"], ["research", "clinical"], "v3")
        self._activate(consent_v3["id"], ["research", "clinical"], "v3")

        stored = self.service.get(sample["id"])
        self.assertFalse(stored["data"]["restricted"])
        self.assertEqual(stored["data"]["restricted_uses"], [])

        loaned = self.service.transition(
            self.actor,
            sample["id"],
            "loan",
            {"recipient": "lab", "purpose": "research", "due_at": "2026-12-31"},
        )
        self.assertEqual(loaned["status"], "on_loan")

    def test_batch_collision_aborts_and_lands_on_one_version(self):
        participant = self._participant()
        consent_v1 = self._consent(participant["id"], ["research"], "v1")
        self._activate(consent_v1["id"], ["research"], "v1")
        version = self.service.get(consent_v1["id"])["version"]
        samples = [
            self.service.create(
                self.actor,
                "sample",
                {
                    "participant_id": participant["id"],
                    "sample_code": "B-%03d" % i,
                    "collected_at": "2026-01-01",
                },
            )
            for i in range(3)
        ]
        items = [
            {"sample_id": s["id"], "freezer": "F1", "position": "P%d" % i}
            for i, s in enumerate(samples)
        ]

        first = self.service.batch_store(
            self.actor,
            consent_v1["id"],
            items[:1],
            expected_consent_version=version,
            idempotency_key="batch-collision",
        )
        self.assertEqual(first["stored"], 1)

        consent_v2 = self._consent(participant["id"], ["research", "clinical"], "v2")
        self._activate(consent_v2["id"], ["research", "clinical"], "v2")

        with self.assertRaises(ConflictError):
            self.service.batch_store(
                self.actor,
                consent_v1["id"],
                items,
                expected_consent_version=version,
                idempotency_key="batch-collision",
            )
        self.assertEqual(self.service.get(samples[1]["id"])["status"], "collected")
        self.assertEqual(self.service.get(samples[2]["id"])["status"], "collected")

        retry = self.service.batch_store(
            self.actor,
            consent_v2["id"],
            items[1:],
            idempotency_key="batch-collision-v2",
        )
        self.assertEqual(retry["stored"], 2)
        self.assertEqual(self.service.get(samples[0]["id"])["data"]["consent_version"], "v1")
        self.assertEqual(self.service.get(samples[1]["id"])["data"]["consent_version"], "v2")
        self.assertEqual(self.service.get(samples[2]["id"])["data"]["consent_version"], "v2")

    def test_batch_retry_after_item_failure_reuses_computed_parts(self):
        participant = self._participant()
        consent_v1 = self._consent(participant["id"], ["research"], "v1")
        self._activate(consent_v1["id"], ["research"], "v1")
        samples = [
            self.service.create(
                self.actor,
                "sample",
                {
                    "participant_id": participant["id"],
                    "sample_code": "B-%03d" % i,
                    "collected_at": "2026-01-01",
                },
            )
            for i in range(3)
        ]
        bad_items = [
            {"sample_id": samples[0]["id"], "freezer": "F1", "position": "P0"},
            {"sample_id": samples[1]["id"], "position": "P1"},
            {"sample_id": samples[2]["id"], "freezer": "F1", "position": "P2"},
        ]
        with self.assertRaises(ValidationError):
            self.service.batch_store(
                self.actor,
                consent_v1["id"],
                bad_items,
                idempotency_key="batch-retry",
            )
        self.assertEqual(self.service.get(samples[0]["id"])["status"], "stored")
        self.assertEqual(self.service.get(samples[1]["id"])["status"], "collected")

        fixed_items = [
            {"sample_id": samples[0]["id"], "freezer": "F1", "position": "P0"},
            {"sample_id": samples[1]["id"], "freezer": "F1", "position": "P1"},
            {"sample_id": samples[2]["id"], "freezer": "F1", "position": "P2"},
        ]
        retry = self.service.batch_store(
            self.actor,
            consent_v1["id"],
            fixed_items,
            idempotency_key="batch-retry",
        )
        self.assertEqual(retry["reused"], 1)
        self.assertEqual(retry["stored"], 2)
        for sample in samples:
            self.assertEqual(self.service.get(sample["id"])["status"], "stored")


class ConsentScopeHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "http.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.server = create_server("127.0.0.1", 0, self.service, RuleEngine(), "static")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _request(self, method, path, body=None, idempotency_key=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        request.add_header("X-User-Id", "admin")
        request.add_header("X-Role", "admin")
        if idempotency_key:
            request.add_header("Idempotency-Key", idempotency_key)
        with urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_http_batch_store_and_restricted_filter(self):
        participant = self._request("POST", "/api/participants", {"name": "Participant"})
        consent = self._request(
            "POST", "/api/consents", {"participant_id": participant["id"], "scope": ["research"]}
        )
        self._request(
            "POST",
            "/api/consents/%s/actions" % consent["id"],
            {"action": "activate", "data": {"scope": ["research"], "version": "v1", "expires_at": "2099-01-01"}},
        )
        samples = [
            self._request(
                "POST",
                "/api/samples",
                {
                    "participant_id": participant["id"],
                    "sample_code": "B-%03d" % i,
                    "collected_at": "2026-01-01",
                },
            )
            for i in range(2)
        ]
        items = [
            {"sample_id": s["id"], "freezer": "F1", "position": "P%d" % i}
            for i, s in enumerate(samples)
        ]
        result = self._request(
            "POST",
            "/api/batches/store",
            {"consent_id": consent["id"], "items": items},
            idempotency_key="http-batch",
        )
        self.assertEqual(result["stored"], 2)

        consent_v2 = self._request(
            "POST", "/api/consents", {"participant_id": participant["id"], "scope": ["clinical"]}
        )
        self._request(
            "POST",
            "/api/consents/%s/actions" % consent_v2["id"],
            {"action": "activate", "data": {"scope": ["clinical"], "version": "v2", "expires_at": "2099-01-01"}},
        )

        restricted = self._request("GET", "/api/samples?restricted=true")
        self.assertEqual(len(restricted["items"]), 2)
        for item in restricted["items"]:
            self.assertEqual(item["data"]["restricted_by_consent_version"], "v2")

        all_samples = self._request("GET", "/api/samples")
        self.assertEqual(len(all_samples["items"]), 2)


if __name__ == "__main__":
    unittest.main()
