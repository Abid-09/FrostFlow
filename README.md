# Cold-chain Delivery and Quality Incident Management

A process-oriented application for coordinating refrigerated-food deliveries, detecting transport risks, and managing quality incidents.

Developed for **02268 — Process-oriented and Event-driven Software Systems**, Technical University of Denmark, **Group 12**.

## Project overview

The system follows refrigerated shipments from warehouse preparation to cafeteria receiving and any subsequent quality investigation, corrective fulfilment or supplier claim. The scenario covers two vehicles, three campus cafeterias and five chilled products: milk, yoghurt, fresh cheese, chicken breast and prepared salad.

Delivery-risk events already support workflow decisions through the local Siddhi app; temperature-excursion and recovery patterns remain planned. Physical receipt is separate from stock admission: goods under investigation must remain on hold until an authorised quality decision permits their release. Vehicle telemetry and physical actions are simulated. Optional TomTom traffic ETAs are implemented; weather integration remains planned.

## Demonstration dataset

A fresh dataset contains **20 crates of each of five products: 100 crates total**. Each crate represents 12 retail packs, with the pack size defined in [the catalogue](deployer/catalog.json). Every shipment contains one product from one batch. Reservations and replenishment use that exact product/batch; total stock cannot substitute for a missing item.

The [dataset reference](docs/RUNNING.md#demonstration-dataset) lists all products, batches, units and stored records. These are simulated quantities and handling assumptions. `CHILLED-DEMO` identifies a shared demonstration profile; product-specific food-safety limits have not been validated or implemented.

**Existing pooled-stock databases require a new dataset.** Preserve their matching engine data and follow [the upgrade/start instructions](docs/RUNNING.md#persistence-isolated-checks-and-troubleshooting); they are not converted automatically.

## Processes

| Process | Execution platform | Status |
| --- | --- | --- |
| P1 — Shipment preparation | Camunda / Zeebe | Stock wait/cancellation, dispatch handoff and local compliance verification implemented |
| P2 — Transport and delivery | Camunda / Zeebe | Receipt-gated transport, scripted GPS/ETA, arrival and dispatcher decisions implemented |
| P3 — Receiving and stock admission | Camunda / Zeebe | P2 arrival integration, physical receipt, inspection, separate receiving inventory and quality hold implemented; four live scenarios and local replay verified; see current external-check status in the run guide |
| P4 — Quality incident investigation | DCR (dcrgraphs.net repository, or local engine on the same graph) | Investigation implemented and connected to the P2 hold handoff and P3 admission; offline tests, dcrgraphs.net simulation and a live stack run verified; dcrgraphs.net API check and ProcessDoctor pending ([P4 guide](docs/P4-QUALITY-INVESTIGATION.md)) |
| P5 — Corrective fulfilment | Camunda / Zeebe | Planned |
| P6 — Supplier claim resolution | Camunda / Zeebe | Planned |

P1 supports shipment creation, stock checking, replenishment or cancellation during shortages, loading verification and dispatch. P2 starts a scripted route, records GPS/ETA updates, raises automatic arrival, detects delivery risk through the local Siddhi app (three distinct consecutive fresh late ETAs), places the shipment's quality status on hold, and waits for P3's physical receipt before releasing the vehicle. P3 receives P2's durable arrival notification, records physical receipt and inspection, then admits passing goods into a separate cafeteria receiving inventory; goods under a quality investigation remain on hold until P4 sends a disposition. Siddhi forwards approved route commands to the local scripted-simulator endpoint and waits for its acknowledgement before P2 updates the route; a route change keeps the destination. TomTom remains optional. Use the [short-deadline walkthrough](docs/RUNNING.md#live-siddhi-risk-and-route-command-walkthrough) for Siddhi-generated risk; CLI option 11 is a manual test input. The Siddhi risk and route-command loop, the P3 receipt gate and the quality hold are live-verified; P2's DCR rules and log are in `compliance/p2/`. P4 consumes the hold's durable handoff, runs the investigation as a DCR case and applies the officer's decision for the current incident, which resumes a held P3 (CLI option 18; option 17 remains a stand-in that bypasses P4). See the [evidence table](docs/RUNNING.md#evidence-what-was-verified-and-where) for what was verified on which commit. See the [P2/P3 handover](docs/COLD-CHAIN-INTEGRATION-AGREEMENT.md#p2-and-p3-development-handover) for interface details and remaining checks.

## Architecture

The transport adapter sends recorded telemetry to a configured Siddhi HTTP endpoint and accepts Siddhi risk alerts. A route-change worker waits for Siddhi's matching simulator acknowledgement before changing the route. `P3_ARRIVAL_URL` defaults to the local P3 receiver; override it only when directing notifications to an external compatible receiver. Local integration modes are reported by `/health`.

The current implementation contains:

- **Camunda Zeebe 8.8** — execution of P1 shipment preparation, P2 transport and P3 receiving.
- **DCR** — execution of the P4 quality investigation: the dcrgraphs.net Active Repository when `DCR_*` credentials are configured, otherwise a local engine on the same graph (reported by `/health`).
- **Python 3.12** — HTTP endpoints and Zeebe job workers, using Flask and pyzeebe.
- **SQLite** — persistent shipment registry, warehouse reservations, separate recipient/product/batch receiving inventory, activity journal and durable outbox.
- **Elasticsearch and Camunda Operate** — exported engine records and process inspection.

## Getting started

Prerequisites: Docker Desktop with Compose, Python 3.12 for the checks, and Camunda Modeler for editing diagrams. BPMN lint commands also use Node.js/npm. The current checkpoint needs no external API keys or `.env` file; an optional untracked `.env` (see `.env.example`) switches P4 to the dcrgraphs.net repository.

Start Docker Desktop, then run these commands from the repository root. Ports 5001, 8006, 8007, 8081, 9200, 9601 and 26501 must be available. Inspect existing stacks/data first and keep only one project stack running; preserve each registry with its matching engine volumes.

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
compliance/          P1–P4 DCR rules, replay tools and reviewed evidence
dcr/                 P4 executable DCR graph, its PDF and the P4 AS-IS model
siddhi-apps/         Local telemetry risk rule and route-command bridge
docs/                Implementation guide, integration agreement and run instructions
project_notes/       Team task board and AI-use declaration record
docker-compose.yml   Local application and workflow infrastructure
```

Directories for additional components will be introduced with their implementations.

### DCR models and compliance rules

`compliance/p1/`, `compliance/p2/` and `compliance/p3/` contain DCR rules that check recorded execution; Camunda runs these three processes. P5/P6 will need their own compliance deliverables as they are implemented.

P4 uses DCR to **execute** the quality investigation. Its model is [`dcr/p4_quality_investigation.xml`](dcr/p4_quality_investigation.xml) ([PDF](dcr/p4_quality_investigation.pdf)); `compliance/p4/rules.xml` is the same graph, with the dcrgraphs.net simulation log, forbidden traces, screenshots and verification report. See the [P4 guide](docs/P4-QUALITY-INVESTIGATION.md).

## Current P2/P3 integration and remaining work

P2's receipt-gated flow is implemented from `ShipmentDispatched` through arrival, receipt acknowledgement, transport completion and vehicle release. P2 sends an idempotent durable arrival notification to P3; P3 records receipt, acknowledges P2, then inspects and admits, holds or rejects goods. Receiving inventory is separate from warehouse inventory. Offline regressions, P2/P3 live acceptance, local DCR replay and merged-main smoke are recorded in the verification reports. See the [P2/P3 handover](docs/COLD-CHAIN-INTEGRATION-AGREEMENT.md#p2-and-p3-development-handover) for contracts and remaining work.

**P1 owner: Ashvanth. P2 owner: Rahul. P3 owner: Arunima Garg. P4: Abid Hasan (claimed with the P4 PR; confirm on the task board).** P5/P6 and shared external integration owners remain unassigned. The P4 dcrgraphs.net API check and ProcessDoctor, fresh P2/P3 ProcessDoctor checks, a current live TomTom recheck, temperature/weather integration and the external simulator remain outstanding. Historical TomTom results are retained separately from current deterministic runs using local ETAs.

## Project documentation

- [Agent instructions](AGENTS.md) — repository context, invariants, workflow and required documentation updates when features or milestones change.
- [Implementation guide](docs/COLD-CHAIN-IMPLEMENTATION-GUIDE.md) — scenario, process scope and responsibilities.
- [Integration agreement](docs/COLD-CHAIN-INTEGRATION-AGREEMENT.md) — identifiers, messages, quality states and shared rules.
- [Run guide](docs/RUNNING.md) — beginner walkthrough, BPMN/DCR explanation and verification.
- [P1 model PDF](bpmn/shipment_preparation.pdf) and [DCR compliance rules](compliance/p1/rules.xml).
- [P4 guide](docs/P4-QUALITY-INVESTIGATION.md) — the DCR investigation, its rules, API and evidence.
- [Team task board](project_notes/TASKS.md) — ownership, progress and remaining deliverables.
- [AI-use record](project_notes/AI-USAGE.md) — assistance recorded for the team contract declaration.

Team members work on feature branches and review changes before merging into `main`. Changes to shared interfaces must update the integration agreement and their affected producers and consumers. Credentials, generated runtime data and supplied course/team PDFs are excluded from version control.
