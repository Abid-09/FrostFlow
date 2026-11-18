import sys,json
from pathlib import Path
sys.path[:0]=['stage_tests']
from check_p3_compliance import http,require,wait_for
from check_stage1 import engine_records
raw=Path('runtime/foundation/review-18')
data=json.loads((raw/'p3-evidence.json').read_text())
results=[]
for c in data['cases']:
 sid=c['shipmentId']; r=require(http('http://localhost:5001',f'/shipments/{sid}'),200,'final state')
 expected=c['expectedFinalStatus']; end='StockAdmitted' if expected=='ADMITTED' else 'GoodsRejected'
 p3=wait_for('P3 end',lambda:engine_records(r['p3ProcessInstanceKey'],end,'ELEMENT_COMPLETED'))
 p2=wait_for('P2 end',lambda:engine_records(r['p2ProcessInstanceKey'],'TransportCompleted','ELEMENT_COMPLETED'))
 assert r['p3Status']==expected and r['vehicleReleased']
 assert r['monitoring']['state']=='CLOSED' and r['monitoring']['finalization'] in ('FINALIZED','HELD')
 types=[e['eventType'] for e in c['events']]
 assert types.count('GoodsReceived')==1 and types.count('GoodsInspected')==1
 assert types.count('StockAdmitted')==(1 if expected=='ADMITTED' else 0)
 results.append({'shipmentId':sid,'scenario':c['scenario'],'finalStatus':expected,'qualityStatus':r['qualityStatus'],'incidentId':r.get('qualityIncidentId'),'evidenceVersion':r.get('evidenceVersion'),'monitoringFinalization':r['monitoring']['finalization'],'p2End':p2,'p3End':p3,'receiptAndInspectionEachOnce':True})
stock=require(http('http://localhost:5001','/stock'),200,'stock')
receiving=require(http('http://localhost:5001','/receiving-stock'),200,'receiving')
assert stock['available']==96,stock
assert [p['available'] for p in stock['products']]==[19,19,19,19,20],stock
assert receiving['available']==2,receiving
(raw/'p3-engine-and-inventory.json').write_text(json.dumps({'status':'passed','warehouseDelta':-4,'receivingDelta':2,'warehouse':stock,'receiving':receiving,'cases':results},indent=2)+'\n')
print('PASS: all four P2/P3 exported end states, one receipt/inspection each, warehouse -4 and receiving +2.')
