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
- BPMN/API/menu wiring, live verification and final documentation: pending.
- ProcessDoctor external conformance: pending new real execution evidence and manual check.
