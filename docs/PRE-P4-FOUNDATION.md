# Pre-P4 foundation requirements and assessment contract

## Requirements checklist and authority

| Authority | Requirement/design | Status at start (main 552945d) |
| --- | --- | --- |
| Course specification and signed contract, as summarized in the implementation guide | Five executable BPMN processes, one executable DCR process, connected execution, real logs and external conformance, reproducible instructions and faithful AI declaration | P1–P3 exist; P4–P6 pending. Original PDFs are absent and have not been inspected in this increment. Final requirements need comparison with the original documents. |
| Proposal, as summarized in the guide | Simulated transport/container data, TomTom traffic ETA, independent Open-Meteo outdoor conditions, Siddhi detection, quality admission permission | GPS/ETA and delay detection exist; temperature/weather and final evaluation gate missing. |
| Approved implementation plan (this increment) | Traceable temperature sessions, weather-supported urgent assessment, durable uncertainty holds, ordered evaluation and versioned future P4 boundary | Implementation and evidence tracked below; no P4 execution or cooling/recovery workflow. |

Historical verification applies only to the revisions and hashes in existing compliance reports. Fixture tests, real engine execution, local DCR replay and ProcessDoctor checks are separate claims.

## Demonstration profile

New shipments snapshot `MONITORING_PROFILE_JSON` overrides over the defaults in `deployer/monitoring.py`. Sessions begin at processed loading verification and close at processed physical receipt, including time waiting after arrival. Existing shipments without a snapshot are legacy/unmonitored. No historical readings are invented.

Defaults: normal 3°C, fault 10°C, chicken assessment above 4°C and other products above 5°C; 5 s sampling; three consecutive distinct fresh observations spanning at least 10 s; maximum 10 s gap; 15 s freshness; 20 s silence and closing-evaluation timeout. Combined risk requires current-revision persistent/fresh lateness, three increasing readings rising at least 0.5°C over 10 s, and eligible outdoor temperature at least 30°C. All are configurable demonstration settings. Ten seconds demonstrates persistence, not spoilage. Neither recovery nor weather grants admission or rejects goods.

General context: [Food Standards Scotland](https://www.foodstandards.gov.scot/consumer-advice/food-safety/food-safety-in-the-kitchen/chilling) advises a fridge range of 0–5°C. This does not validate product-specific transport safety limits or the chicken trigger. The numeric triggers and shortened durations come from the approved demonstration plan, not a validated safety study.

[Open-Meteo current conditions](https://open-meteo.com/en/docs) are model-based outdoor data, not container measurements. Request temperature, precipitation, wind speed and weather code, with UTC data time, retrieval time, requested/provider coordinates, units and input mode. Cache successful responses 600 s by 0.05° grid and attempt once per journey per 60 s. Combined-risk eligibility requires age ≤1800 s and distance ≤10 km; unchanged provider observations retain their identity and are not new evidence. Failure is unavailable.

## Future P4 assessment extension v1

Retain `DeliveryRiskIncident` and the `riskEvent` fields for delay handoffs. Every monitored assessment adds `assessment` with `schemaVersion: 1`, reason, priority (`normal`/`urgent`), evidenceVersion, profile/session/observation references, interval, ETA/weather context, explicit missingEvidence, source/input modes and timestamps. The envelope retains deliveryId, eventId, shipmentId, journeyId, productId, batchId, incidentId, incidentOpened and current qualityStatus. P4 HTTP retries reuse the delivery ID and immutable payload. The consumer must return a matching `deliveryId` and `status: accepted` or `duplicate` only after durable acceptance. Unconfigured/failed/unacknowledged P4 delivery remains pending with the hold intact.

Repeated qualifying evidence coalesces into the current pending or rejected incident. Material new reason/priority/episode increments evidenceVersion; priority escalates once and never falls within that incident. After release, a newly qualifying episode can create a new incident. Terminal rejection is preserved. For monitored shipments a disposition must name the current incident and `expectedEvidenceVersion`; stale dispositions fail. Test-disposition senders are explicitly stand-ins, never P4 execution.

Container evaluation acknowledgements identify stored eventId, shipmentId, journeyId and sequence and include excursion/combined results. Conflicting retries fail. Out-of-order acknowledgements are retained but applied only through a contiguous sequence. Risk effects and the contiguous cursor advance in one transaction. Admission requires a closed, finalized monitoring session and current quality permission in the admission transaction. Closure unresolved for 20 s creates a durable monitoring-evaluation hold before finalization. Physical receipt acknowledges P2 independently of this assessment wait.
