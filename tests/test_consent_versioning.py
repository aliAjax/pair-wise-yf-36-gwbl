import threading
import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, RestrictedUseError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ConsentVersioningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.committee = Actor("board", "committee")
        self.researcher = Actor("scientist", "researcher")
        self.biobank = Actor("bank", "biobank")
        self._seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self):
        self.participant = self.service.create(
            self.admin, "participant", {"name": "Participant One"}
        )
        self.consent_v1 = self.service.create(
            self.committee,
            "consent",
            {"participant_id": self.participant["id"], "scope": ["research", "anonymization"]},
        )
        self.consent_v1 = self.service.transition(
            self.committee,
            self.consent_v1["id"],
            "activate",
            {"scope": ["research", "anonymization"], "version": "v1",
             "expires_at": "2099-01-01"},
        )

    def _store_sample(self, code, actor=None):
        actor = actor or self.biobank
        sample = self.service.create(
            self.biobank,
            "sample",
            {"participant_id": self.participant["id"], "sample_code": code,
             "collected_at": "2026-01-01"},
        )
        return self.service.transition(
            actor, sample["id"], "store",
            {"freezer": "F1", "position": code, "consent_id": self.consent_v1["id"]},
        )

    def _create_v2(self, scope=("research",)):
        v2 = self.service.create(
            self.committee,
            "consent",
            {"participant_id": self.participant["id"],
             "scope": list(scope)},
        )
        return self.service.transition(
            self.committee, v2["id"], "activate",
            {"scope": list(scope), "version": "v2", "expires_at": "2099-01-01"},
        )

    def test_new_consent_recomputes_range_and_pauses_uses(self):
        sample = self._store_sample("B-001")
        # Both uses work before the new consent takes effect.
        loaned = self.service.transition(
            self.biobank, sample["id"], "loan",
            {"recipient": "lab A", "purpose": "anonymization", "due_at": "2099-02-01"},
        )
        self.assertEqual(loaned["status"], "on_loan")
        sample = self.service.transition(self.biobank, sample["id"], "return", {})

        result = self._create_v2()
        self.assertEqual(result["recomputed_samples"], 1)

        sample = self.service.get(sample["id"])
        self.assertEqual(sample["data"]["availability"], "restricted")
        self.assertEqual(sample["data"]["restricted_purposes"], ["anonymization"])
        # The basis version behind the restriction is visible on the sample.
        self.assertEqual(sample["data"]["restricted_basis"]["version"], "v2")
        self.assertEqual(sample["data"]["consent_version"], "v1")

        # Loans for covered purposes are also paused while restricted.
        with self.assertRaises(RestrictedUseError):
            self.service.transition(
                self.biobank, sample["id"], "loan",
                {"recipient": "lab B", "purpose": "research", "due_at": "2099-02-01"},
            )
        # Anonymization is paused too.
        with self.assertRaises(RestrictedUseError):
            self.service.transition(
                self.biobank, sample["id"], "anonymize", {"reason": "study"}
            )

        # Old consent is superseded; activation writes restriction audit rows.
        old = self.service.get(self.consent_v1["id"])
        self.assertEqual(old["status"], "superseded")
        audit = self.service.audit_log(sample["id"])
        actions = [row["action"] for row in audit]
        self.assertIn("restrict", actions)
        self.assertEqual(
            [row for row in audit if row["action"] == "restrict"][0]["detail"]["basis"]["version"],
            "v2",
        )

    def test_countersign_restores_restricted_samples(self):
        sample = self._store_sample("B-002")
        v2 = self._create_v2()
        sample = self.service.get(sample["id"])
        self.assertEqual(sample["data"]["availability"], "restricted")

        updated = self.service.transition(
            self.biobank, v2["id"], "countersign", {}
        )
        self.assertEqual(updated["restored_samples"], 1)

        sample = self.service.get(sample["id"])
        self.assertEqual(sample["data"]["availability"], "available")
        self.assertEqual(sample["data"]["restricted_purposes"], [])
        self.assertIsNone(sample["data"]["restricted_basis"])
        # Re-based onto the new consent version.
        self.assertEqual(sample["data"]["consent_id"], v2["id"])
        self.assertEqual(sample["data"]["consent_version"], "v2")

        # Loans work again for the covered purpose.
        loaned = self.service.transition(
            self.biobank, sample["id"], "loan",
            {"recipient": "lab B", "purpose": "research", "due_at": "2099-02-01"},
        )
        self.assertEqual(loaned["status"], "on_loan")

    def test_batch_failure_is_resumable(self):
        items = [
            {"ref": "ok-1", "participant_id": self.participant["id"],
             "sample_code": "BX-1", "collected_at": "2026-01-01",
             "freezer": "F1", "position": "A1"},
            {"ref": "bad-1", "participant_id": "someone-else",
             "sample_code": "BX-2", "collected_at": "2026-01-01",
             "freezer": "F1", "position": "A2"},
        ]
        first = self.service.batch_store_samples(
            self.researcher, self.consent_v1["id"], items, batch_id="job-1"
        )
        self.assertEqual(first["status"], "partial")
        self.assertEqual(first["stored_count"], 1)
        self.assertEqual(first["failed_count"], 1)
        self.assertEqual(first["consent_version"], "v1")

        # Retry: resubmit the corrected failed item; the stored item keeps
        # its computed result and is not stored a second time.
        retry_items = [
            {"ref": "ok-1", "participant_id": self.participant["id"],
             "sample_code": "BX-1", "collected_at": "2026-01-01",
             "freezer": "F1", "position": "A1"},
            {"ref": "bad-1", "participant_id": self.participant["id"],
             "sample_code": "BX-2", "collected_at": "2026-01-01",
             "freezer": "F1", "position": "A2", "consent_id": "x"},
        ]
        # First fix the actual problem: the bad item was fine except a
        # simulated issue — mark position properly via a valid payload.
        retry_items[1] = {"ref": "bad-1", "participant_id": self.participant["id"],
                          "sample_code": "BX-2", "collected_at": "2026-01-01",
                          "freezer": "F2", "position": "B2"}
        second = self.service.batch_store_samples(
            self.researcher, self.consent_v1["id"], retry_items, batch_id="job-1"
        )
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["stored_count"], 2)
        self.assertEqual(second["failed_count"], 0)
        self.assertEqual(len(second["stored"]), 2)

        job = self.service.get_batch("job-1")
        self.assertEqual({item["status"] for item in job["items"]}, {"stored"})
        samples = self.service.list("sample")
        self.assertEqual(len(samples), 2)
        self.assertTrue(
            all(s["data"]["consent_version"] == "v1" for s in samples)
        )

    def _batch_payload(self, start, count):
        return [
            {"ref": "s-%d" % i, "participant_id": self.participant["id"],
             "sample_code": "C-%d" % i, "collected_at": "2026-01-01",
             "freezer": "F1", "position": "P%d" % i}
            for i in range(start, start + count)
        ]

    def test_batch_racing_activation_lands_on_one_version(self):
        # Run activation and batch storage concurrently many times; in every
        # run the batch must land on exactly one consent version.
        for run in range(5):
            tmp = tempfile.TemporaryDirectory()
            repo = SQLiteRepository(Path(tmp.name) / "race.db")
            service = DomainService(repo, RuleEngine())
            try:
                participant = service.create(
                    self.admin, "participant", {"name": "P %d" % run}
                )
                v1 = service.create(
                    self.committee,
                    "consent",
                    {"participant_id": participant["id"],
                     "scope": ["research", "anonymization"]},
                )
                v1 = service.transition(
                    self.committee, v1["id"], "activate",
                    {"scope": ["research", "anonymization"], "version": "v1-%d" % run,
                     "expires_at": "2099-01-01"},
                )
                v2_draft = service.create(
                    self.committee,
                    "consent",
                    {"participant_id": participant["id"], "scope": ["research"]},
                )
                errors = []

                def run_batch():
                    try:
                        service.batch_store_samples(
                            Actor("sci", "researcher"), v1["id"],
                            [
                                {"ref": "s-%d" % i,
                                 "participant_id": participant["id"],
                                 "sample_code": "R%d-%d" % (run, i),
                                 "collected_at": "2026-01-01",
                                 "freezer": "F", "position": "P%d" % i}
                                for i in range(10)
                            ],
                            batch_id="race-%d" % run,
                        )
                    except ConflictError as exc:
                        errors.append(exc)
                    except Exception as exc:  # pragma: no cover - surfaces races
                        errors.append(exc)

                def run_activate():
                    try:
                        service.transition(
                            self.committee, v2_draft["id"], "activate",
                            {"scope": ["research"], "version": "v2-%d" % run,
                             "expires_at": "2099-01-01"},
                        )
                    except ConflictError:
                        pass

                t1 = threading.Thread(target=run_batch)
                t2 = threading.Thread(target=run_activate)
                t1.start(); t2.start()
                t1.join(); t2.join()

                self.assertFalse(
                    any(not isinstance(e, ConflictError) for e in errors),
                    "unexpected error: %r" % errors,
                )
                samples = repo.find_entities(
                    "sample", "participant_id", participant["id"]
                )
                versions = {s["data"]["consent_version"] for s in samples}
                # Never split across two versions: either the whole batch
                # landed on v1, or activation won and nothing was stored.
                self.assertTrue(
                    versions in (set(), {"v1-%d" % run}),
                    "batch split across versions: %r" % versions,
                )
                if versions == {"v1-%d" % run}:
                    self.assertEqual(len(samples), 10)
                    # All v1 samples were recomputed: anonymization restricted.
                    self.assertTrue(
                        all(s["data"]["availability"] == "restricted" for s in samples)
                    )
            finally:
                tmp.cleanup()

    def test_batch_rejected_after_consent_superseded(self):
        self._create_v2()
        with self.assertRaises(ConflictError):
            self.service.batch_store_samples(
                self.researcher,
                self.consent_v1["id"],
                self._batch_payload(0, 3),
                batch_id="late-batch",
            )
        # Nothing stored.
        self.assertEqual(self.service.list("sample"), [])


if __name__ == "__main__":
    unittest.main()
