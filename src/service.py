from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError, ValidationError
from .repository import utcnow
from .rules import MANAGED_SAMPLE_FIELDS, RuleEngine, _snapshot_storage


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "consent" and action == "activate":
            return self._activate_consent(actor, entity, dict(data or {}), expected_version)
        if kind == "consent" and action == "countersign":
            return self._countersign_consent(actor, entity, dict(data or {}), expected_version)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    # ------------------------------------------------------------------
    # Consent lifecycle: activation recompute and countersign recovery
    # ------------------------------------------------------------------

    def _activate_consent(self, actor, consent, data, expected_version):
        """Activate a draft consent, supersede the prior one, and recompute
        the usable range of samples stored under the prior version.

        The activation and every sample update share one BEGIN IMMEDIATE
        transaction, so a batch storage racing with activation either
        completes fully against the old version or fails outright.
        """
        # Rule checks (role, required fields, scope/participant validity)
        # happen before the write lock is taken.
        next_status, patch = self.rules.validate_transition(
            actor, consent, "activate", data, self._lookup
        )
        expected = int(expected_version) if expected_version is not None else consent["version"]
        if consent["status"] != "draft":
            raise InvalidTransition("cannot activate consent from status " + consent["status"])
        affected = 0
        with self.repository.transaction() as conn:
            row = conn.execute(
                "SELECT version FROM entities WHERE id = ?", (consent["id"],)
            ).fetchone()
            if not row or int(row["version"]) != expected:
                raise ConflictError(
                    "version conflict: expected %s" % expected
                )
            merged = dict(consent["data"])
            merged.update(patch)
            new_consent = self.repository.update_entity(
                consent["id"], expected, next_status, merged, conn=conn
            )
            self.repository.append_audit(
                consent["id"], actor.user_id, actor.role, "activate",
                consent["status"], next_status, {"patch": patch}, conn=conn,
            )
            prior = self.repository.find_entities(
                "consent", "participant_id", new_consent["data"]["participant_id"], conn=conn
            )
            for old in prior:
                if old["id"] == new_consent["id"] or old["status"] != "active":
                    continue
                old_data = dict(old["data"])
                old_data["superseded_by"] = new_consent["id"]
                old_data["supersede_reason"] = "superseded by newer activation"
                self.repository.update_entity(
                    old["id"], int(old["version"]), "superseded", old_data, conn=conn
                )
                self.repository.append_audit(
                    old["id"], actor.user_id, actor.role, "supersede",
                    "active", "superseded",
                    {"reason": "superseded by newer activation", "by": new_consent["id"]},
                    conn=conn,
                )
                affected += self._restrict_samples(
                    conn, actor, old, new_consent, reason="activation"
                )
            result = dict(new_consent)
            result["recomputed_samples"] = affected
        return result

    def _restrict_samples(self, conn, actor, old_consent, new_consent, reason):
        """Mark uses the new consent scope no longer covers as restricted."""
        new_scope = set(new_consent["data"].get("scope", []))
        basis = {
            "consent_id": new_consent["id"],
            "version": new_consent["data"].get("version"),
        }
        samples = self.repository.find_entities(
            "sample", "participant_id", new_consent["data"]["participant_id"], conn=conn
        )
        affected = 0
        for sample in samples:
            if sample["status"] not in ("stored", "on_loan"):
                continue
            if sample["data"].get("consent_id") != old_consent["id"]:
                continue
            granted = sample["data"].get("granted_purposes") or old_consent["data"].get(
                "scope", []
            )
            restricted = sorted(set(granted) - new_scope)
            if not restricted:
                continue
            data = dict(sample["data"])
            data["availability"] = "restricted"
            data["restricted_purposes"] = restricted
            data["restricted_basis"] = basis
            data["granted_purposes"] = sorted(set(granted) & new_scope)
            self.repository.update_entity(
                sample["id"], int(sample["version"]), sample["status"], data, conn=conn
            )
            self.repository.append_audit(
                sample["id"], actor.user_id, actor.role, "restrict",
                sample["status"], sample["status"],
                {"restricted_purposes": restricted, "basis": basis, "reason": reason},
                conn=conn,
            )
            affected += 1
        return affected

    def _countersign_consent(self, actor, consent, data, expected_version):
        """Participant (via biobank/admin) countersigns the new consent.
        Samples that were restricted by the activation are re-based onto the
        new consent version and become available again.
        """
        next_status, patch = self.rules.validate_transition(
            actor, consent, "countersign", data, self._lookup
        )
        if consent["status"] != "active":
            raise InvalidTransition(
                "cannot countersign consent from status " + consent["status"]
            )
        expected = int(expected_version) if expected_version is not None else consent["version"]
        restored = 0
        with self.repository.transaction() as conn:
            row = conn.execute(
                "SELECT version FROM entities WHERE id = ?", (consent["id"],)
            ).fetchone()
            if not row or int(row["version"]) != expected:
                raise ConflictError("version conflict: expected %s" % expected)
            merged = dict(consent["data"])
            merged.update(patch)
            merged["countersigned"] = True
            merged["countersigned_by"] = actor.user_id
            merged["countersigned_at"] = data.get("signed_at") or utcnow()
            updated = self.repository.update_entity(
                consent["id"], expected, next_status, merged, conn=conn
            )
            self.repository.append_audit(
                consent["id"], actor.user_id, actor.role, "countersign",
                "active", "active", {"patch": patch}, conn=conn,
            )
            restored = self._restore_samples(conn, actor, updated)
            result = dict(updated)
            result["restored_samples"] = restored
        return result

    def _restore_samples(self, conn, actor, consent):
        scope = list(consent["data"].get("scope", []))
        basis_version = consent["data"].get("version")
        samples = self.repository.find_entities(
            "sample", "participant_id", consent["data"]["participant_id"], conn=conn
        )
        restored = 0
        for sample in samples:
            if sample["status"] not in ("stored", "on_loan"):
                continue
            data = sample["data"]
            if data.get("availability") != "restricted":
                continue
            new_data = dict(data)
            new_data["availability"] = "available"
            new_data["restricted_purposes"] = []
            new_data["restricted_basis"] = None
            new_data["consent_id"] = consent["id"]
            new_data["consent_version"] = basis_version
            new_data["granted_purposes"] = sorted(
                set(new_data.get("granted_purposes", [])) & set(scope)
            )
            self.repository.update_entity(
                sample["id"], int(sample["version"]), sample["status"], new_data, conn=conn
            )
            self.repository.append_audit(
                sample["id"], actor.user_id, actor.role, "restore",
                sample["status"], sample["status"],
                {"reason": "countersign", "consent_id": consent["id"],
                 "consent_version": basis_version},
                conn=conn,
            )
            restored += 1
        return restored

    # ------------------------------------------------------------------
    # Batch storage: one consent version per batch, resumable on retry
    # ------------------------------------------------------------------

    def batch_store_samples(self, actor, consent_id, items, batch_id=None):
        """Store a batch of samples under one consent version.

        Items are validated independently and persisted as batch_items.
        On retry the same batch_id reuses items already stored and only
        re-attempts the failed ones. The consent is pinned for the whole
        batch: if it has been superseded since the batch was created, the
        retry is rejected so the batch never lands across two versions.
        """
        if actor.role not in self.rules.BATCH_STORE_ROLES:
            from .domain import PermissionDenied
            raise PermissionDenied("role %s is not allowed here" % actor.role)
        if not items:
            raise ValidationError("items are required")
        refs = [str(item.get("ref", "")) for item in items]
        if any(not ref for ref in refs):
            raise ValidationError("each item requires a ref")
        if len(set(refs)) != len(refs):
            raise ValidationError("duplicate refs in batch")

        job_id = batch_id or str(uuid4())
        with self.repository.transaction() as conn:
            job = self.repository.get_batch_job(job_id)
            consent = self.repository.get_entity(consent_id, conn=conn)
            if not consent or consent["kind"] != "consent":
                raise ValidationError("unknown consent: " + consent_id)
            if job is None:
                # New batch: the pinned consent must be active right now.
                if consent["status"] != "active":
                    raise ConflictError(
                        "consent %s is not active (status=%s); batch cannot start"
                        % (consent_id, consent["status"])
                    )
                participant_ids = {item.get("participant_id") for item in items}
                participant_id = (
                    consent["data"].get("participant_id")
                    if participant_ids == {consent["data"].get("participant_id")}
                    else None
                )
                job = {
                    "id": job_id,
                    "kind": "sample_store",
                    "status": "running",
                    "consent_id": consent_id,
                    "participant_id": participant_id,
                    "total": len(items),
                    "stored_count": 0,
                    "failed_count": 0,
                    "created_by": actor.user_id,
                }
                self.repository.create_batch_job(job, conn)
            else:
                # Retry: envelope must match and must stay on one version.
                if job["consent_id"] != consent_id:
                    raise ConflictError(
                        "batch is pinned to consent %s" % job["consent_id"]
                    )
                if consent["status"] != "active":
                    raise ConflictError(
                        "consent %s was %s before this batch finished; "
                        "the batch stays on one version and is rejected"
                        % (consent_id, consent["status"])
                    )
                if job["kind"] != "sample_store":
                    raise ConflictError("batch id is not a sample_store batch")

            snapshot = _snapshot_storage({}, consent)
            existing_items = {
                row["ref"]: row
                for row in self.repository.list_batch_items(job_id, conn=conn)
            }

            stored_ids = []
            failed = []
            stored_count = sum(
                1 for row in existing_items.values() if row["status"] == "stored"
            )
            submitted = set()

            for item in items:
                ref = str(item["ref"])
                submitted.add(ref)
                prior = existing_items.get(ref)
                if prior and prior["status"] == "stored":
                    # Already handled: keep the computed result.
                    stored_ids.append(prior["sample_id"])
                    continue
                payload = {k: v for k, v in item.items() if k != "ref"}
                error = self._validate_batch_item(payload, consent, conn)
                if error:
                    failed.append({"ref": ref, "error": error})
                    self.repository.upsert_batch_item(
                        job_id,
                        {"ref": ref, "status": "failed", "error": error,
                         "sample_id": None, "payload": payload},
                        conn,
                    )
                    continue
                sample_id = str(payload.pop("id", "") or uuid4())
                data = {k: v for k, v in payload.items() if k not in MANAGED_SAMPLE_FIELDS}
                data.update(snapshot)
                sample = self.repository.create_entity(
                    sample_id, "sample", "stored", data, actor.user_id, conn=conn
                )
                self.repository.append_audit(
                    sample_id, actor.user_id, actor.role, "batch_store",
                    None, "stored",
                    {"batch_id": job_id, "consent_id": consent_id,
                     "consent_version": consent["data"].get("version")},
                    conn=conn,
                )
                self.repository.upsert_batch_item(
                    job_id,
                    {"ref": ref, "status": "stored", "error": None,
                     "sample_id": sample_id, "payload": payload},
                    conn,
                )
                stored_ids.append(sample_id)
                stored_count += 1

            # Items not resubmitted keep their previous row; count all rows.
            all_rows = self.repository.list_batch_items(job_id, conn=conn)
            failed_count = sum(1 for row in all_rows if row["status"] == "failed")
            status = "completed" if failed_count == 0 else "partial"
            self.repository.update_batch_job(
                job_id, status, stored_count, failed_count, conn
            )

        final_job = self.repository.get_batch_job(job_id)
        return {
            "batch_id": job_id,
            "status": status,
            "consent_id": consent_id,
            "consent_version": consent["data"].get("version"),
            "stored": stored_ids,
            "failed": failed,
            "stored_count": final_job["stored_count"],
            "failed_count": final_job["failed_count"],
        }

    def _validate_batch_item(self, payload, consent, conn):
        for field in ("participant_id", "sample_code", "collected_at",
                      "freezer", "position"):
            if payload.get(field) in (None, "", [], {}):
                return "missing required field: " + field
        if payload["participant_id"] != consent["data"].get("participant_id"):
            return "participant_id does not match pinned consent"
        if "research" not in consent["data"].get("scope", []):
            return "consent does not include research use"
        sample_id = payload.get("id")
        if sample_id and self.repository.get_entity(sample_id, conn=conn):
            return "sample already exists: " + str(sample_id)
        existing = self.repository.find_entities(
            "sample", "sample_code", payload["sample_code"], conn=conn
        )
        if existing:
            return "duplicate sample_code: " + str(payload["sample_code"])
        return None

    def get_batch(self, batch_id):
        job = self.repository.get_batch_job(batch_id)
        if not job:
            raise NotFoundError("batch not found: " + batch_id)
        items = self.repository.list_batch_items(batch_id)
        result = dict(job)
        result["items"] = items
        return result

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
