from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError, ValidationError
from .rules import RuleEngine


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
        if entity["kind"] == "consent" and action == "activate":
            self._after_consent_activated(actor, updated)
        return updated

    def _after_consent_activated(self, actor, consent):
        participant_id = consent["data"].get("participant_id")
        for other in self.repository.list_entities(kind="consent"):
            if (
                other["id"] != consent["id"]
                and other["data"].get("participant_id") == participant_id
                and other["status"] == "active"
            ):
                self.repository.update_entity(
                    other["id"], other["version"], "superseded", dict(other["data"])
                )
                self.audit.record(
                    other["id"],
                    actor,
                    "supersede",
                    "active",
                    "superseded",
                    {"reason": "new consent %s activated" % consent["id"]},
                )
        self._recalculate_samples(actor, consent)

    def _recalculate_samples(self, actor, consent):
        participant_id = consent["data"].get("participant_id")
        new_scope = set(consent["data"].get("scope") or [])
        consent_version = consent["data"].get("version")
        for sample in self.repository.list_entities(kind="sample"):
            if sample["data"].get("participant_id") != participant_id:
                continue
            if sample["status"] != "stored":
                continue
            sample_scope = set(sample["data"].get("scope") or [])
            restricted_uses = sorted(sample_scope - new_scope)
            was_restricted = bool(sample["data"].get("restricted"))
            before_uses = set(sample["data"].get("restricted_uses") or [])
            if bool(restricted_uses) == was_restricted and set(restricted_uses) == before_uses:
                continue
            data = dict(sample["data"])
            if restricted_uses:
                data["restricted"] = True
                data["restricted_uses"] = restricted_uses
                data["restricted_by_consent_id"] = consent["id"]
                data["restricted_by_consent_version"] = consent_version
            else:
                data["restricted"] = False
                data["restricted_uses"] = []
                data.pop("restricted_by_consent_id", None)
                data.pop("restricted_by_consent_version", None)
            self.repository.update_entity(sample["id"], sample["version"], sample["status"], data)
            self.audit.record(
                sample["id"],
                actor,
                "recalculate_scope",
                sample["status"],
                sample["status"],
                {
                    "consent_id": consent["id"],
                    "consent_version": consent_version,
                    "restricted_uses": restricted_uses,
                },
            )

    def batch_store(self, actor, consent_id, items, expected_consent_version=None, idempotency_key=None):
        consent = self.repository.get_entity(consent_id)
        if not consent or consent["kind"] != "consent":
            raise NotFoundError("consent not found: " + str(consent_id))
        if expected_consent_version is not None and int(expected_consent_version) != int(consent["version"]):
            raise ConflictError(
                "consent version conflict: expected %s, found %s"
                % (expected_consent_version, consent["version"])
            )
        if consent["status"] != "active":
            raise ValidationError("consent is not active")
        batch_key = idempotency_key or str(uuid4())
        results = []
        stored = 0
        reused = 0
        for index, item in enumerate(items or []):
            item_key = "%s#%d" % (batch_key, index)
            existing = self.repository.get_idempotency(actor.user_id, item_key)
            if existing:
                sample = self.repository.get_entity(existing)
                if sample:
                    results.append({"index": index, "sample_id": existing, "status": "reused"})
                    reused += 1
                    continue
            current = self.repository.get_entity(consent_id)
            if current["status"] != "active":
                raise ConflictError("consent changed during batch; aborting")
            if expected_consent_version is not None and int(current["version"]) != int(expected_consent_version):
                raise ConflictError("consent version changed during batch; aborting")
            sample = self.repository.get_entity(item.get("sample_id"))
            if not sample or sample["kind"] != "sample":
                raise NotFoundError("sample not found: " + str(item.get("sample_id")))
            if sample["status"] != "collected":
                raise InvalidTransition("sample %s is not collected" % sample["id"])
            if sample["data"].get("participant_id") != consent["data"].get("participant_id"):
                raise ValidationError("sample does not belong to consent participant")
            item_data = dict(item)
            item_data["consent_id"] = consent_id
            next_status, patch = self.rules.validate_transition(
                actor, sample, "store", item_data, self._lookup
            )
            merged = dict(sample["data"])
            merged.update(patch)
            updated = self.repository.update_entity(sample["id"], sample["version"], next_status, merged)
            self.audit.record(
                sample["id"], actor, "store", sample["status"], updated["status"],
                {"batch": batch_key, "index": index},
            )
            self.repository.save_idempotency(actor.user_id, item_key, sample["id"])
            results.append({"index": index, "sample_id": sample["id"], "status": "stored"})
            stored += 1
        return {"batch_key": batch_key, "stored": stored, "reused": reused, "results": results}

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None, restricted=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status, restricted=restricted)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
