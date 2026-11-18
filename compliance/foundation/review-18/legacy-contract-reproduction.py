"""Offline reproduction of PR #10 contract mismatches; DCR calls are substituted.

Run from the repository root with the existing .venv. No live registry, engine,
DCR service, credentials or source files are changed.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path[:0] = [str(Path.cwd() / 'deployer')]
from registry import Registry
from transport_integrations import validate_p4_acknowledgement

REVISION = 'af3c68c1e98e6d3245cfdea9c7ade36bbae32a5d'


class DcrStub:
    mode = 'offline-test-stub'
    def create_simulation(self, **kwargs):
        return 'offline-case'
    def events(self, case):
        return {'events': [{'id': 'OpenIncident', 'enabled': True}]}
    def execute(self, *args):
        pass


def main():
    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)
        for name in ['quality.py', 'dcr_client.py']:
            (temp / name).write_bytes(subprocess.check_output(['git', 'show', f'{REVISION}:deployer/{name}']))
        sys.path.insert(0, directory)
        spec = importlib.util.spec_from_file_location('legacy_quality', temp / 'quality.py')
        legacy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(legacy)
        registry = Registry(temp / 'registry.sqlite')
        registry.create(dict(shipmentId='LEGACY-CONTRACT', journeyId='J-LEGACY-CONTRACT', vehicleId='V-01',
                             recipientId='C-01', productId='PRD-001', batchId='DEMO-MILK-001', quantity=1,
                             routeId='route-01', deliveryDeadline='2026-11-18T12:00:00Z'))
        with registry.transaction() as db:
            record = registry.read(db, 'LEGACY-CONTRACT')
            record.update(p2ProcessInstanceKey='offline-fixture', transportState='IN_TRANSIT')
            registry.save(db, record)
        registry.queue_delivery_risk('LEGACY-CONTRACT', 'risk-001', {'occurredAt':'2026-11-18T12:01:00Z'})
        delay = registry.pending_messages('p4-http')[0]['variables']
        service = legacy.QualityService(lambda: registry, lambda: DcrStub())
        acknowledgement = service.accept_handoff(delay, delay['event']['deliveryId'])
        try:
            validate_p4_acknowledgement(acknowledgement, delay['event']['deliveryId'])
        except ValueError as error:
            ack_failure = str(error)
        else:
            raise AssertionError('Legacy acknowledgement unexpectedly satisfies foundation contract')
        with registry.transaction() as db:
            record = registry.read(db, 'LEGACY-CONTRACT')
            registry.assessment_hold(db, record, 'temperature-excursion', {
                'eventId':'temperature-001', 'occurredAt':'2026-11-18T12:02:00Z'})
            registry.save(db, record)
        assessment = registry.pending_messages('p4-http')[-1]['variables']
        try:
            service.accept_handoff(assessment, assessment['event']['deliveryId'])
        except ValueError as error:
            intake_failure = str(error)
        else:
            raise AssertionError('Legacy intake unexpectedly accepts AssessmentRequested')
        incident_id = delay['event']['incidentId']
        with registry.transaction() as db:
            incident = legacy.read_incident(db, incident_id)
            incident.update(dispositionApplied=False, decision={
                'decision':'RELEASE', 'officer':'offline-test', 'reason':'Recorded decision fixture',
                'decidedAt':'2026-11-18T12:03:00Z'})
            legacy.save_incident(db, incident)
        service._apply_disposition(incident_id)
        with registry.transaction() as db:
            incident = legacy.read_incident(db, incident_id)
        record = registry.get('LEGACY-CONTRACT')
        assert record['qualityStatus'] == 'PENDING'
        assert incident['dispositionApplied'] is True
        assert 'expectedEvidenceVersion' in incident['decision']['notApplied']
        result = {'status':'three-contract-mismatches-reproduced', 'legacyRevision':REVISION,
                  'foundationRevision':subprocess.check_output(['git','rev-parse','8d13c3e'],text=True).strip(),
                  'mode':'offline real temporary SQLite; engine state seeded; DCR calls substituted; no live/P4 execution claim',
                  'intake':{'wireEventType':assessment['event']['eventType'],'failure':intake_failure},
                  'acknowledgement':{'returnedKeys':list(acknowledgement),'failure':ack_failure},
                  'disposition':{'foundationQualityStatus':record['qualityStatus'],
                                 'evidenceVersion':record['evidenceVersion'],
                                 'legacyDispositionApplied':incident['dispositionApplied'],
                                 'notApplied':incident['decision']['notApplied']}}
        print(json.dumps(result,indent=2))


if __name__ == '__main__':
    main()
