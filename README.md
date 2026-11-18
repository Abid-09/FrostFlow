# Cold-chain Delivery and Quality Incident Management

**18 November sequencing decision:** Abid’s P4 implementation exists in [PR #10](https://github.com/ash-01xor/FrostFlow/pull/10) and Git history. The pre-P4 baseline was restored by PR #11 while foundation [PR #9](https://github.com/ash-01xor/FrostFlow/pull/9) is reviewed. P4 integration is deferred until that foundation is merged and smoke-tested; references below to outstanding P4 work mean adaptation and reintegration. See [sequencing and verification](docs/P4-INTEGRATION-SEQUENCING.md). PR #11 is merged; PR #9 still requires separate review and explicit merge approval.

A process-oriented application for coordinating refrigerated-food deliveries, detecting transport risks, and managing quality incidents.

Developed for **02268 — Process-oriented and Event-driven Software Systems**, Technical University of Denmark, **Group 12**.

## Project overview

The system follows refrigerated shipments from warehouse preparation to cafeteria receiving and any subsequent quality investigation, corrective fulfilment or supplier claim. The scenario covers two vehicles, three campus cafeterias and five chilled products: milk, yoghurt, fresh cheese, chicken breast and prepared salad.

Delivery-risk events support workflow decisions through the local Siddhi app. Container excursion and combined transport-risk assessment are implemented; recovery remains planned. Physical receipt is separate from stock admission: goods under investigation must remain on hold until an authorised quality decision permits their release. Vehicle telemetry and physical actions are simulated. Optional TomTom traffic ETAs are implemented; Open-Meteo current outdoor conditions support urgent combined-risk assessment.

## Demonstration dataset

A fresh dataset contains **20 crates of each of five products: 100 crates total**. Each crate represents 12 retail packs, with the pack size defined in [the catalogue](deployer/catalog.json). Every shipment contains one product from one batch. Reservations and replenishment use that exact product/batch; total stock cannot substitute for a missing item.

The [dataset reference](docs/RUNNING.md#demonstration-dataset) lists all products, batches, units and stored records. These are simulated quantities and handling assumptions. `CHILLED-DEMO` identifies a shared demonstration profile; new shipments snapshot configurable assessment triggers, whose product-specific food-safety suitability has not been validated.

**Existing pooled-stock databases require a new dataset.** Preserve their matching engine data and follow [the upgrade/start instructions](docs/RUNNING.md#persistence-isolated-checks-and-troubleshooting); they are not converted automatically.

## Processes

| Process | Execution platform | Status |
| --- | --- | --- |
| P1 — Shipment preparation | Camunda / Zeebe | Stock wait/cancellation, dispatch handoff and local compliance verification implemented |
| P2 — Transport and delivery | Camunda / Zeebe | Receipt-gated transport, scripted GPS/ETA, arrival and dispatcher decisions implemented |
| P3 — Receiving and stock admission | Camunda / Zeebe | P2 arrival integration, physical receipt, inspection, separate receiving inventory and quality hold implemented; four live scenarios and local replay verified; see current external-check status in the run guide |
| P4 — Quality incident investigation | DCR | Abid’s PR #10 implementation deferred pending foundation adaptation |
| P5 — Corrective fulfilment | Camunda / Zeebe | Planned |
| P6 — Supplier claim resolution | Camunda / Zeebe | Planned |

P1 supports shipment creation, stock checking, replenishment or cancellation during shortages, loading verification and dispatch. P2 starts a scripted route, records GPS/ETA updates, raises automatic arrival, detects delivery risk through the local Siddhi app (three distinct consecutive fresh late ETAs), places the shipment's quality status on hold, and waits for P3's physical receipt before releasing the vehicle. P3 receives P2's durable arrival notification, records physical receipt and inspection, then admits passing goods into a separate cafeteria receiving inventory; goods under a quality investigation remain on hold until P4 sends a disposition. Siddhi forwards approved route commands to the local scripted-simulator endpoint and waits for its acknowledgement before P2 updates the route; a route change keeps the destination. TomTom remains optional. Use the [short-deadline walkthrough](docs/RUNNING.md#live-siddhi-risk-and-route-command-walkthrough) for Siddhi-generated risk; CLI option 11 is a manual test input. The Siddhi risk and route-command loop, the P3 receipt gate and the quality hold are live-verified; P2's DCR rules and log are in `compliance/p2/`. **P4 is deferred from this baseline pending foundation adaptation**: the hold's P4 handoff is queued and the release is a stand-in (CLI option 17). See the [evidence table](docs/RUNNING.md#evidence-what-was-verified-and-where) for what was verified on which commit. See the [P2/P3 handover](docs/COLD-CHAIN-INTEGRATION-AGREEMENT.md#p2-and-p3-development-handover) for interface details and remaining checks.

## Architecture

The transport adapter sends recorded telemetry to a configured Siddhi HTTP endpoint and accepts Siddhi risk alerts. A route-change worker waits for Siddhi's matching simulator acknowledgement before changing the route. `P3_ARRIVAL_URL` defaults to the local P3 receiver; override it only when directing notifications to an external compatible receiver. Local integration modes are reported by `/health`.

The current implementation contains:

- **Camunda Zeebe 8.8** — execution of P1 shipment preparation, P2 transport and P3 receiving.
- **Python 3.12** — HTTP endpoints and Zeebe job workers, using Flask and pyzeebe.
- **SQLite** — persistent shipment registry, warehouse reservations, separate recipient/product/batch receiving inventory, activity journal and durable outbox.
- **Elasticsearch and Camunda Operate** — exported engine records and process inspection.

## Getting started

Prerequisites: Docker Desktop with Compose, Python 3.12 for the checks, and Camunda Modeler for editing diagrams. BPMN lint commands also use Node.js/npm. The current checkpoint needs no external API keys or `.env` file.

Start Docker Desktop, then run these commands from the repository root. Ports 5001, 8006, 8007, 8008, 8081, 9200, 9601 and 26501 must be available. Inspect existing stacks/data first and keep only one project stack running; preserve each registry with its matching engine volumes.

```bash
docker compose up --build -d --wait --wait-timeout 180
curl http://localhost:5001/health
```

Wait for the health endpoint to report `ready`. Open **Operate** at <http://localhost:8081> using the local development credentials `demo` / `demo`.

Follow the [run guide](docs/RUNNING.md) to create a shipment, verify loading and dispatch it. The guide also covers API behaviour, testing, persistence and local data resets.

For interactive demonstration controls after startup:

```bash
python3 tools/demo.py
```

The menu performs application actions and prints the corresponding instance keys to inspect in Operate. See [presentation controls](docs/RUNNING.md#presentation-controls) for the rehearsal sequence.

To stop the stack while preserving its data:

```bash
docker compose stop
```

Resume the existing containers with `docker compose start`, then check `/health` before opening the demo. Stopping preserves orders, stock and engine history. The menu uses preset order quantities (A: 15 crates; B/C: 10) to make the documented shortage scenario repeatable; the API accepts other quantities.

## Testing

The [18 November foundation review](compliance/foundation-review-18.json) records 82 passing offline tests and fresh sequential P1/P2/P3, container, weather-priority, outage and restart checks at `8d13c3e`. ProcessDoctor remains pending; foundation PR #9 is not merged and needs its own approval followed by merged-main smoke. Earlier reports retain their original tested revisions.

Run the offline API and registry tests with Python 3.12:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r deployer/requirements.txt
.venv/bin/python -m unittest discover -s stage_tests -p 'test_*.py' -v
```

For the P1 engine regression, use fresh paired version-3 registry/engine data (all 100 crates, no shipments, free vehicles) and disable the simulator. Follow the [sequential verification setup](docs/RUNNING.md#automated-verification) before running:

```bash
.venv/bin/python stage_tests/check_stage1.py
```

The live integration check exercises P1 stock wait/cancellation/replenishment and completes two independent P2 journeys through manual arrival and physical receipt recorded by P3. It confirms early receipt is rejected, exact retries do not duplicate activities, both transports reach the `TransportCompleted` end event, and a released vehicle can be reused. For an existing dataset with sufficient stock, use `stage_tests/check_integration_smoke.py` instead of repeating the fresh-data suite; see the run guide for prerequisites.

The offline suite covers P1/P2 behavior and P3 arrival deduplication, receipt acknowledgement, inspection, stock admission and quality holds. P2's three live journeys and P3's four live scenarios pass local DCR replay; the P3 forbidden-admission check is rejected as required. Evidence is under `compliance/p2/` and `compliance/p3/`. Fresh ProcessDoctor checks for the current P2/P3 logs remain pending; historical portal results apply only to their original logs. P1 evidence is under `compliance/p1/` and its XES contains P1 activities only. `runtime/verification-results/p2-completion.json` records live P2 execution, not DCR conformance. The [merged-main report](compliance/merged-main-verification.json) records the successful P1–P3 smoke test.

## Repository structure

```text
bpmn/                Executable BPMN models and PDF exports
deployer/            HTTP integration, Zeebe workers and shipment registry
event_streams/       Scenario inputs
stage_tests/         Offline tests and engine integration checks
tools/               Interactive demonstration controls
compliance/          P1/P2/P3 DCR rules, replay tools and reviewed evidence
siddhi-apps/         Local telemetry risk rule and route-command bridge
docs/                Implementation guide, integration agreement and run instructions
project_notes/       Team task board and AI-use declaration record
docker-compose.yml   Local application and workflow infrastructure
```

Directories for additional components will be introduced with their implementations.

### DCR models and compliance rules

`compliance/p1/`, `compliance/p2/` and `compliance/p3/` contain DCR rules that check recorded execution; Camunda runs these three processes. P4–P6 will need their own compliance deliverables as they are implemented.

Abid’s P4 implementation uses DCR to **execute** the quality investigation. Its `dcr/` models and shared-service integration from PR #10 are retained in history for restoration and adaptation after foundation verification.

## Current P2/P3 integration and remaining work

P2's receipt-gated flow is implemented from `ShipmentDispatched` through arrival, receipt acknowledgement, transport completion and vehicle release. P2 sends an idempotent durable arrival notification to P3; P3 records receipt, acknowledges P2, then inspects and admits, holds or rejects goods. Receiving inventory is separate from warehouse inventory. Offline regressions, P2/P3 live acceptance, local DCR replay and merged-main smoke are recorded in the verification reports. See the [P2/P3 handover](docs/COLD-CHAIN-INTEGRATION-AGREEMENT.md#p2-and-p3-development-handover) for contracts and remaining work.

**P1 owner: Ashvanth. P2 owner: Rahul. P3 owner: Arunima Garg.** Abid owns P4; P5/P6 and shared external integration owners remain unassigned. P4's real investigation/consumer, ProcessDoctor refreshes for changed foundation evidence, recovery and the external simulator remain outstanding. Historical TomTom results are retained separately from current deterministic runs using local ETAs.

## Project documentation

- [Agent instructions](AGENTS.md) — repository context, invariants, workflow and required documentation updates when features or milestones change.
- [Implementation guide](docs/COLD-CHAIN-IMPLEMENTATION-GUIDE.md) — scenario, process scope and responsibilities.
- [Integration agreement](docs/COLD-CHAIN-INTEGRATION-AGREEMENT.md) — identifiers, messages, quality states and shared rules.
- [Run guide](docs/RUNNING.md) — beginner walkthrough, BPMN/DCR explanation and verification.
- [P4 handover to Abid](docs/P4-FOUNDATION-HANDOVER.md) — monitored assessment intake, durable acknowledgements, evidence versions and contract fixtures.
- [P1 model PDF](bpmn/shipment_preparation.pdf) and [DCR compliance rules](compliance/p1/rules.xml).
- [Team task board](project_notes/TASKS.md) — ownership, progress and remaining deliverables.
- [AI-use record](project_notes/AI-USAGE.md) — assistance recorded for the team contract declaration.

Team members work on feature branches and review changes before merging into `main`. Changes to shared interfaces must update the integration agreement and their affected producers and consumers. Credentials, generated runtime data and supplied course/team PDFs are excluded from version control.


## Pre-P4 monitoring foundation

See the [requirements and assessment contract](docs/PRE-P4-FOUNDATION.md) and [run instructions](docs/RUNNING.md#container-monitoring-foundation). New shipments monitor from processed loading verification through physical receipt. Container observations are persisted and evaluated by Siddhi; silence or unresolved closing evaluation creates a durable assessment hold. P3 finalizes monitoring before checking current quality permission and admitting stock. Holds do not delay P2 vehicle release after receipt.

CLI option 18 selects normal, isolated-spike, sustained-excursion or missing-telemetry scenarios. Open-Meteo outdoor heat can raise a combined assessment to urgent when fresh persistent ETA lateness and rising container readings coincide. Weather cannot replace container measurements or reject goods. The versioned retry-safe P4 handoff remains queued without a consumer; CLI dispositions are labelled stand-ins and require current incident/evidence version. P4–P6 execution, cooling, recovery and incident closure are excluded. [Foundation verification](compliance/foundation-verification.json) separates recorded fixtures, live providers, live engine execution and pending external conformance.
