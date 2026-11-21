# P5 — Corrective fulfilment

## Implementation plan — 21 November 2026

Work is local on branch `P5`, starting at `347ae74`; the user will review and publish.
Human process ownership remains unassigned. P6 claims, cooling/recovery CEP and external
physical simulation remain outside this change.

1. Persist one corrective case per applied P4 `REPLACE`/`RETURN` disposition and queue
   its Zeebe message atomically with the disposition journal. A quality-status test
   stand-in is insufficient authorization. Release creates no corrective case.
2. Deploy `CorrectiveFulfilment`: record authorization, await an explicit arrangement,
   then track original collection and replacement fulfilment in parallel. Replacement
   preparation calls the existing P1 process with a new shipment/journey and the same
   product, batch, quantity and recipient; P2/P3 run through their existing handoffs.
3. Require the matching original collection booking and physical-receipt evidence.
   Await P3 stock admission for replacement success. Cancelled or rejected replacements
   produce a failed corrective outcome, never successful fulfilment. No rejected stock
   is returned to warehouse or receiving inventory.
4. Add retry-safe API/menu controls, offline fault/concurrency coverage and a fresh-data
   live runner. Supply BPMN/PDF, DCR compliance rules, forbidden traces and reproducible
   evidence tooling. Preserve previous reviewed evidence.
5. Review README, run guide, integration agreement, implementation guide, task board and
   AGENTS.md; record actual checks/revisions and remaining external checks. Commit each
   coherent step; do not push, publish or merge.

Camunda's [8.8 call activity contract](https://docs.camunda.io/docs/8.8/components/modeler/bpmn/call-activities/)
supports deployment binding and explicit variable mappings. P5 will use deployment
binding and suppress child-variable propagation to preserve the original shipment's
identity in the parent process.

## Progress

- Planning: repository/contracts read; branch clean and based on current local main.
- Durable effects implemented: one case per applied disposition, transactional P4/P3
  outboxes, replacement identity/vehicle reservation, original collection and terminal
  success/failure guards. Public `/shipments` cannot forge replacement links.
- Eight new offline tests cover monitored P1–P4 authorization, replacement admission and
  rejection, shortage cancellation, retry/restart, concurrent arrangement, forged input
  and P4 transaction rollback/recovery. These substitute Zeebe; live checks remain pending.
- BPMN/API/menu wiring implemented. The model lints with zero issues; its exported PDF
  was rendered and visually inspected. Three API regressions bring the offline suite
  to 129 passing tests. CLI option 20 manages P5, and existing shipment IDs can be used
  for replacement loading/dispatch/receipt/inspection.
- Added the [fresh-data live runner](../stage_tests/check_p5_fulfilment.py) and
  [compliance builder/rules](../compliance/p5/). The runner covers return, admitted and
  rejected replacements, and cancellation after naturally exhausted stock. It checks
  exact inventory, retries/refusals, original rejection, P5 engine end records and the
  called P1 child's parent key. The compliance graph permits both parallel orderings
  and rejects ten synthetic forbidden traces. All 134 offline tests pass.
- Live verification, captured compliance evidence and final documentation: pending.
- First development run `649b1605` passed all four live scenarios at `27f324b` (application
  image sources from `ce3646d`, identical at that revision). Raw evidence remains in
  `runtime/p5-compliance/evidence.json` with matched `p5-01` data. This run used inspection
  failure for the rejected replacement. It does not validate subsequent review fixes.
- Review added an independent `replacementForCorrectiveId`, so a replacement's own P4
  return cannot overwrite its parent link, plus authorization-time and P3 arrival-reference
  guards. Two new regressions bring the suite to 136 tests. The live runner now exercises
  the replacement's own Siddhi/P4 rejection and completes its separate return as well.
  Final fresh-data verification of these changes is pending.

See the [wire/API contract](COLD-CHAIN-INTEGRATION-AGREEMENT.md#p5-corrective-fulfilment-contract)
and [BPMN](../bpmn/corrective_fulfilment.bpmn)/[PDF](../bpmn/corrective_fulfilment.pdf).

## Live verification commands

Inspect existing containers/volumes first; keep at most one stack running. On a fresh
matched dataset, with the existing stopped datasets preserved:

```bash
export COMPOSE_PROJECT_NAME=frostflow-p5 VERIFY_DATASET=p5-01 REGISTRY_DIR=./runtime/p5/p5-01
export P2_SIMULATOR_ENABLED=true GPS_TICK_SECONDS=2 TOMTOM_API_KEY= DCR_MODE=local
docker compose -f docker-compose.yml -f stage_tests/verification.compose.yml up --build -d --wait --wait-timeout 180
.venv/bin/python stage_tests/check_p5_fulfilment.py --base http://localhost:5001
.venv/bin/python compliance/p5/build_p5_evidence.py live runtime/p5-compliance/evidence.json
.venv/bin/python compliance/check.py compliance/p5/execution-log.xes --model compliance/p5/rules.xml
```

This suite consumes 25 warehouse crates and admits exactly one yoghurt crate; the
20-crate chicken original forces its replacement into P1's real shortage wait. Reuse
these variables to resume that dataset; select **both** new registry and volume identities
for another fresh run. The live runner refuses reused datasets/evidence paths. The
builder refuses partial/synthetic evidence and existing output; select `--output-dir`
for subsequent results. It never fabricates an execution log from the forbidden traces.
Exported P5 completion, local replay and external ProcessDoctor are separate checks.
- ProcessDoctor external conformance: pending new real execution evidence and manual check.
