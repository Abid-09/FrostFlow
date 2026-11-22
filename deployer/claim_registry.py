"""Durable supplier claims, driven by the P6 Zeebe workflow."""

import json
from uuid import uuid4


class ClaimRegistry:
    @staticmethod
    def init_claims(db):
        db.execute(
            "CREATE TABLE IF NOT EXISTS claims (id TEXT PRIMARY KEY, "
            "incident_id TEXT NOT NULL UNIQUE, data TEXT NOT NULL)"
        )

    @staticmethod
    def read_claim(db, claim_id):
        row = db.execute("SELECT data FROM claims WHERE id=?", (claim_id,)).fetchone()
        if row is None:
            raise KeyError(claim_id)
        return json.loads(row[0])

    @staticmethod
    def save_claim(db, claim):
        db.execute("UPDATE claims SET data=? WHERE id=?", (json.dumps(claim), claim["claimId"]))

    def get_claim(self, claim_id):
        with self.transaction() as db:
            return self.read_claim(db, claim_id)

    def accept_claim(self, loss):
        from registry import Conflict

        with self.transaction() as db:
            row = db.execute(
                "SELECT data FROM claims WHERE incident_id=?", (loss["incidentId"],)
            ).fetchone()
            if row:
                claim = json.loads(row[0])
                if claim["loss"] != loss:
                    raise Conflict("incident already has a different recorded claimable loss")
                return claim

            shipment = self.read(db, loss["originalShipmentId"])
            if shipment.get("qualityIncidentId") != loss["incidentId"]:
                raise Conflict("incident must match the shipment's current quality incident")

            claim_id = uuid4().hex
            claim = dict(claimId=claim_id, loss=loss, status="ACCEPTED")
            db.execute(
                "INSERT INTO claims VALUES (?,?,?)",
                (claim_id, loss["incidentId"], json.dumps(claim)),
            )
            self.claim_event(db, claim, "ClaimableLossRecorded", loss, loss["actor"], "manual")
            self.enqueue(db, f"P6:{claim_id}:start", "ClaimableLossRecorded", claim_id,
                         {"claimId": claim_id})
            return claim

    def claim_event(self, db, claim, activity, payload, actor="system", mode="internal"):
        loss = claim["loss"]
        shipment = self.read(db, loss["originalShipmentId"])
        shipment.update(claimId=claim["claimId"], p6ProcessInstanceKey=claim.get("processInstanceKey"),
                        incidentId=loss["incidentId"], originalShipmentId=loss["originalShipmentId"])
        self.event(
            db, shipment, activity, actor=actor, process="P6",
            event_id=f"P6:{claim['claimId']}:{activity}",
            payload={**payload, "claimId": claim["claimId"], "incidentId": loss["incidentId"],
                     "originalShipmentId": loss["originalShipmentId"]},
            source="supplier-claims", input_mode=mode,
        )

    def assemble_claim(self, claim_id, key):
        from registry import Conflict

        with self.transaction() as db:
            claim = self.read_claim(db, claim_id)
            if claim["status"] == "ACCEPTED":
                claim.update(processInstanceKey=key, status="EVIDENCE_READY")
                evidence = {"evidenceRefs": claim["loss"]["evidenceRefs"],
                            "lossDescription": claim["loss"]["lossDescription"]}
                claim["evidence"] = evidence
                self.claim_event(db, claim, "ClaimEvidenceAssembled", evidence)
                self.save_claim(db, claim)
            elif claim["processInstanceKey"] != key:
                raise Conflict("claim belongs to a different process instance")

    def submit_claim(self, claim_id, key):
        from registry import Conflict

        with self.transaction() as db:
            claim = self.read_claim(db, claim_id)
            if claim.get("processInstanceKey") != key:
                raise Conflict("claim belongs to a different process instance")
            if claim["status"] == "EVIDENCE_READY":
                claim["status"] = "SUBMITTED"
                self.claim_event(db, claim, "SupplierClaimSubmitted", {}, mode="simulated")
                self.save_claim(db, claim)

    def accept_supplier_response(self, claim_id, response):
        from registry import Conflict

        with self.transaction() as db:
            claim = self.read_claim(db, claim_id)
            if "supplierResponse" in claim:
                if claim["supplierResponse"] != response:
                    raise Conflict("claim already has a different supplier response")
                return claim
            if claim["status"] != "SUBMITTED":
                raise Conflict("claim must be submitted before accepting a response")
            claim["supplierResponse"] = response
            self.save_claim(db, claim)
            self.claim_event(db, claim, "SupplierResponseRecorded", response,
                             response["actor"], "simulated")
            self.enqueue(db, f"P6:{claim_id}:response", "SupplierResponseRecorded", claim_id, {})
            return claim

    def close_claim(self, claim_id, key):
        from registry import Conflict

        with self.transaction() as db:
            claim = self.read_claim(db, claim_id)
            if claim.get("processInstanceKey") != key:
                raise Conflict("claim belongs to a different process instance")
            if claim["status"] == "CLOSED":
                return claim["outcome"]
            if claim["status"] != "SUBMITTED" or "supplierResponse" not in claim:
                raise Conflict("claim closure requires a recorded supplier response")
            response = claim["supplierResponse"]
            claim.update(status="CLOSED", outcome=response["outcome"])
            self.claim_event(db, claim, "SupplierClaimClosed", {"outcome": claim["outcome"]})
            self.save_claim(db, claim)
            return claim["outcome"]
