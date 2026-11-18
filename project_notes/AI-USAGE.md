# AI assistance record

The signed team contract requires a faithful declaration of actual prompts, tools and results. This entry starts that record; team members must review the output, understand it and add subsequent use. Preserve the original session transcript separately if needed for exact prompts.

## 2026-11-01 — repository foundation and P1 scaffold

**Tool:** OpenAI Codex.

**Prompt summary (paraphrase):** Help a beginner team start its cold-chain coursework repository using the existing guide and the sibling EquipmentLoan structure. Write a suitable README, put shared tasks/progress in a tracked subfolder, and begin the first part before owners are assigned.

**Context read:** course project specification, cold-chain proposal, signed team contract, existing implementation guide/integration agreement, EquipmentLoan implementation and documentation, DCR example README, Camunda BPMN skill and upstream documentation. The guides existed before this session; this entry does not assert their authorship.

**AI-produced output:** repository layout, README, progress/requirements/Git notes, P1 BPMN XML, Python HTTP handlers/workers/SQLite journal, Compose packaging, example request, offline tests and live-check script. Codex ran BPMN lint, Compose validation, 10 offline tests, the live P1 engine check and post-restart assertions. The [run guide](../docs/RUNNING.md#automated-verification) records the verification results. No food-safety validation, external API integration or conformance results were produced.

**Human review pending:** inspect the changes, reproduce the real-engine scenario, check assumptions against course expectations, explain the model/worker/message relationship and adapt the implementation as a team. Record the reviewing members and any changes here. AI assistance does not replace each member's required process contribution or understanding.

## 2026-11-01 — documentation cleanup

**Tool:** OpenAI Codex.

**Prompt summary (paraphrase):** Treat this as the official team and submission repository; remove unnecessary files and Markdown, retain useful team context, and polish the README for the project.

**Output:** rewrote the project README, consolidated run instructions and implementation/verification notes into `docs/RUNNING.md`, and removed duplicate documentation, placeholder component READMEs and the machine-specific run snapshot. Retained the existing implementation/integration guides, task board and this declaration record. Application code and process models were unchanged.

## 2026-11-01 — P1 exception flow, P2 handoff and compliance

**Tool:** OpenAI Codex.

**Prompt summary (paraphrase):** Implement the stock-wait/replenishment/cancellation flow, protected P2 dispatch handoff and P1 DCR compliance model discussed separately, with verification and a beginner walkthrough. Clarification limited cancellation to stock waiting, left damaged-packaging handling outside scope, and required actual engine runs, model PDF and log conformance evidence.

**Output:** updated P1 BPMN and its PDF; initial P2 BPMN/PDF; inventory/replenishment/cancellation handlers; durable command/handoff outbox and P2 acknowledgement/inbox; flat DCR XML and local replay/XES tooling; separately labelled invalid trace; offline/live checks; reviewed actual XES execution evidence; updates to the existing guides/README/task board. The original 100-crate dataset and vehicle reservations are retained on registry upgrade; isolated data was used for new scenarios.

**Verification:** 13 offline tests, two BPMN lint checks, model PDF export/render inspection, and real Zeebe normal/cancelled/replenished cases. Actual logs were replayed against the XML; the deliberately invalid trace was rejected. Results are in `compliance/p1/verification.json`. No processdoctor import or P4 DCR-PAIS execution is claimed. The team must review, reproduce, understand and attribute its members' process contributions.

**Review fixes:** Codex addressed unroutable shipment IDs, compliance verification accepting unparsed logs, and links to separately supplied PDFs. Added regression checks for rejected IDs without side effects, supported ID routing, malformed/unsupported evidence and successful saved-log replay. All 17 offline tests, focused Python checks and Compose validation passed; live-engine scenarios were not repeated for these validation/documentation changes.

**Presentation support:** At the team's request for a clear three-minute demonstration, Codex added `tools/demo.py`, an interactive terminal menu using the existing HTTP endpoints, and documented the rehearsal sequence in the existing run guide. A live isolated Zeebe rehearsal checked normal dispatch/P2 acknowledgement, blocked early dispatch, cancellation and replenishment. No new business process or external API was implemented.

**First-commit review:** Codex reviewed code and documentation for teammate handover, preserved later P2 progress on registration retries, and tightened rejection of unsupported compliance models/evidence. Added regression coverage (19 offline tests total) and P2/P3 extension instructions to the existing integration agreement. Verified the 26 candidate files in a clean temporary copy with freshly installed dependencies, new engine data, both BPMN lints, Compose validation, actual P1/P2 scenarios, XES replay, service restart and stack recreation. Refreshed reviewed evidence and source/model hashes. External processdoctor import and teammate model review remain pending. No commit or push was performed.

## 2026-11-01 — five-product dataset

**Tool:** OpenAI Codex.

**Prompt summary (paraphrase):** Replace unnamed stock with five distinct chilled items, adopt product/batch-specific inventory and document/update the implementation consistently.

**Output:** catalogue of milk, yoghurt, fresh cheese, chicken breast and prepared salad (20 crates each, 12 retail packs per crate); persisted product/batch stock, shipment validation and identities, targeted replenishment, product-aware menu controls, updated scenario/tests and XES attributes. Updated the existing guides and task board. Legacy pooled registries are preserved and rejected rather than assigned invented product identities. Existing engine evidence remains historical; no food-safety validation is claimed. The shared CHILLED-DEMO profile has no numeric range yet.

**Verification:** 28 offline tests (including menu selection), Python compilation, catalogue JSON and Compose validation; updated engine run and submission-evidence refresh remain pending. No existing containers, engine volumes or runtime datasets were changed.

## 2026-11-01 — publication readiness review

**Tool:** OpenAI Codex.

**Prompt summary (paraphrase):** Review the official repository before the team creates its GitHub repository, confirm teammate setup and documentation, remove unnecessary material, resolve issues and explain publishing steps.

**Output and checks:** tightened startup instructions with Compose readiness waiting and documented pause/resume, preset demo quantities and feature-branch collaboration. Added a portable Ruff-cache ignore and removed an obsolete pre-stock-check compatibility state. Extended live checks to verify P2 product/batch engine variables and untouched product inventory. Reviewed only the 27 candidate files in a clean temporary copy with fresh Python dependencies: 28 offline tests, both BPMN lints, Compose validation, current five-product engine scenarios, XES/DCR replay and persistence after stack recreation passed. Refreshed reviewed execution evidence and matching source/model hashes. Existing practice datasets were preserved; the temporary review stack was removed. External course-tool import and teammate review remain pending. No commit, remote creation or push was performed.

## 2026-11-01 — ProcessDoctor XML compatibility

**Tool:** OpenAI Codex, local headless Chromium/Puppeteer and the actual ProcessDoctor website.

**Prompt summary (paraphrase):** Resolve the XML import error on processdoctor.compute.dtu.dk before publishing the repository.

**Correction:** inspected the live site's importer and added required portal event metadata, layout coordinates and an empty subprocess resource section to `compliance/p1/rules.xml`. Confirmed the logical events, all 53 relationships and initial marking match the prior model exactly. Application code, BPMN definitions and the real execution log were unchanged. Added a portal-metadata regression test; 29 local tests pass.

**Actual site verification:** in an isolated headless browser, selected Conformance → Open Model → Open DCR Solution XML, imported the corrected graph (nine events/53 relations), uploaded the real three-trace XES log and clicked Check. All three variants were Conforming with zero structural violations. A separately generated XES representation of the existing synthetic cancellation-then-dispatch trace was Partially Violating with one structural violation. Saved both result screenshots and recorded the observed outcome, hashes and metadata-only correction in `compliance/p1/verification.json`. Updated the site URL and exact reproduction steps in the existing docs. No new Zeebe run was claimed for this display-format correction; no commit/push was performed. Human ownership attribution and teammate review remain separate completion tasks.

**Ownership attribution:** at the user's explicit request, recorded Ashvanth as P1's responsible student in the task board, README/guide, BPMN documentation, model PDF and DCR title. Regenerated and inspected the attributed PDF, linted P1, preserved executable/logical semantics and recorded the attribution-only hash changes. Removed duplicate collaboration/review wording; P2–P6 remain unassigned and teammate review remains pending.

**Publication preparation:** at the user's request, removed the student attribution from the P1 BPMN source/PDF, retained project ownership records and the DCR title, and regenerated the PDF. P1 BPMN source now exactly matches the engine-verified hash again. BPMN lint, 29 offline tests and Compose validation passed. Preserved the user's task-board edits and prepared the initial commit for the supplied FrostFlow GitHub remote.

## 2026-11-14 — P2 Siddhi live verification and P2 DCR compliance

**Tool:** Claude Code (Claude Opus 5.5).

**Prompt summary (paraphrase):** Help implement the remaining P2 work in the `p2-updates` pull request: verify that the Siddhi app loads and that the live risk-alert and route-command flow completes, and deliver the outstanding P2 items.

**Defects found and fixed:** (1) `ColdChainTelemetry.siddhi` never deployed. SiddhiQL requires all stream definitions before the first query, so the app was reordered. (2) The `http` and `http-service` sources cannot share port 8006, so route commands moved to 8007 (Compose updated). (3) GPS events lack ETA fields and were dropped by the JSON mapper; `fail.on.missing.attribute='false'` was added. (4) The `http-call-response` status regex `2\d+` is not unescaped by SiddhiQL and never matched, so it is now `200`. (5) Siddhi serialises a null `routeId` as `"undefined"`, which made the simulator reject `AcceptDelay`; the receiver now normalises it, with a regression test. (6) pyzeebe closed its gRPC channel after 10 failed calls while a fresh Zeebe was still starting; the client and worker now retry without that limit.

**AI-produced output:** the fixes above; `stage_tests/check_p2_siddhi.py` (live two-shipment acceptance check); the P2 DCR graph, builder, real execution log, synthetic invalid traces and verification report under `compliance/p2/`; 5 new offline tests; documentation and task-board updates. Actual results: the live check passed on a fresh stack, both real traces were valid and accepting, all 4 invalid traces were rejected, and 40 offline tests passed under Python 3.12.16. ProcessDoctor, TomTom, the P3 receiver and an external simulator were **not** verified. Nothing was committed or pushed.

**Human review pending:** Rahul to review the DCR rules against the intended P2 behaviour, rerun the live check, inspect the instances in Operate, and run the ProcessDoctor import.

## 2026-11-15 — TomTom live verification

**Tool:** Claude Code (Claude Opus 5.5).

**Prompt summary (paraphrase):** Do the outstanding items that do not depend on teammates; Rahul supplied his personal TomTom developer key.

**AI-produced output:** the TomTom adapter now reports failures or unusable responses as unavailable ETAs (`tomtom-unavailable`) instead of raising, runs TomTom calls off the async worker loop, labels the final route point `at-destination`, and logs each request, with the key removed, to `tomtom-responses.jsonl` beside the registry. There are 3 new offline tests (44 total, Python 3.12). Actual result: the live acceptance check passed with traffic-aware TomTom ETAs, with 15/15 HTTP 200 responses saved to `compliance/p2/tomtom-responses.jsonl`. The key was used only as a runtime environment variable and appears in no file. The demo routes still use Paris coordinates. A draft P3 integration proposal for Arunima Garg was written; nothing was sent.

## 2026-11-15 — P2/P3 integration on `p2-updates`

**Tool:** Claude Code (Claude Opus 5.5).

**Prompt summary (paraphrase):** Merge Arunima's P3 branch into `p2-updates`, check whether that completes the integration, and finish it in the same PR (Rahul chose a single combined PR).

**AI-produced output:** fast-forwarded `p2-updates` to `feat/p3-receiving-admission`, keeping Arunima Garg's commit and authorship. Merged the separately parked TomTom work, resolving 3 text/test conflicts by keeping both sides. Found that P2's manual receipt input still bypassed P3, and closed it: the endpoint refuses it while P3 receiving is enabled, and the registry refuses it for any shipment P3 has started. CLI option 10 now points to option 14. Both live checks now record receipt through P3 and assert that the manual path is refused. Added 2 offline tests (51 total, Python 3.12). Actual results: `check_p2_siddhi.py` passed on a fresh stack with P3 and TomTom enabled, `check_stage1.py` passed with the simulator off, and the new run's P2 log conforms to `compliance/p2/rules.xml`. Nothing was pushed.

**Human review pending:** Arunima Garg should review the change to how her receipt endpoint is used.

## 2026-11-16 — PR review fixes (a)–(e)

**Tool:** Claude Code (Claude Opus 5.5).

**Prompt summary (paraphrase):** Fix the five PR review comments (route destination, Camunda route variables, delivery-risk quality hold, Siddhi persistence rule, documentation reconciliation).

**AI-produced output (commit `d613960` plus docs):**
- **(a)** Routes are corridors to the recipient's location. A change is planned from the current position to the same destination, and incompatible routes are rejected.
- **(b)** The decision worker returns `routeId`/`routeRevision`.
- **(c)** Accepting a risk persists a `PENDING` hold and a durable P4 handoff in one transaction, and adds CLI option 17 as a P4 stand-in release.
- **(d)** Siddhi alerts after three consecutive fresh late ETAs within 180 s; `check_siddhi_risk_rule.py` was added.
- **(e)** RUNNING/TASKS/README/agreement were reconciled, with an evidence table tied to the tested commit.

**Actual results on `d613960`:** 54 offline tests OK, the Siddhi rule check passed, `check_p2_siddhi.py` passed (run `130506`, local demo ETAs), `check_stage1.py` passed, and the regenerated P2 log replays with 3 of 3 conforming.

**Not done:** P4 consumption of the handoff (it depends on the P4 owner), a ProcessDoctor re-run for the new log, and a TomTom re-check (the key now returns HTTP 401). Nothing was pushed.

## 2026-11-17 — P2/P3 repair and integration verification

**Tool:** OpenAI Codex, local Python 3.12 environment, Docker Compose, GitHub CLI, pinned Camunda BPMN linter; Camunda BPMN skill used for model validation. No sub-agents were used.

**Prompt summary:** implement the supplied plan to repair terminal rejection, duplicate ETA counting and P3 runner readiness; reconcile branches, verify offline and live, refresh evidence/documentation and merge in P2/P3 order subject to verification and repository review requirements.

**Assistance:** inspected PR review findings and stopped-stack mounts/data; implemented the P2 rejection/evidence repair and Siddhi event-identity deduplication, added regressions and the P3 loading wait; reconciled README/task-board conflicts retaining both branches' work; ran offline, model and live checks; regenerated affected logs and updated verification reports and run instructions. Fresh registry/engine pairs were used sequentially in one verification stack. Existing registry/engine data was preserved. Exact outcomes and tested commits are recorded in `compliance/p2/verification.json` and `compliance/p3/verification.json`.

**Limits:** ProcessDoctor browser access failed with “The browser access host is unavailable”; no fresh portal result or screenshot is claimed. P4 quality dispositions remain a labelled test stand-in; a real P4 DCR investigation and P5–P6 were not implemented. Team members must review and understand the changes and reproduce the checks.

**Completion:** after the user explicitly chose “Merge now; retain external check as pending”, Codex merged PR #4, updated PR #5 against main and inspected its remaining diff, merged PR #5, and smoke-tested merged main on the same stack. Added a reusable smoke helper and the compact merged-main verification report. Normal admission and hold/rejection reached exported P2/P3 end events with exact stock deltas. Existing reviews were retained and the three repaired defect threads resolved. Fresh ProcessDoctor checks remain pending.

**Follow-up delivery:** at the user’s request, the reusable P1/P2/P3 smoke helper, final evidence and documentation were moved to `test/p1-p2-p3-integration-smoke` for a separate review PR. No follow-up commit was pushed directly to main.

## 2026-11-17 — repository agent instructions

**Tool:** OpenAI Codex. **Prompt summary:** inspect repository guidelines and requirements and create a root agent-context file for future contributors.

**Output:** added `AGENTS.md`, covering source documents, implemented versus planned P1–P6 scope, architecture, business/message invariants, branch/PR workflow, verification levels, paired Docker/registry data, evidence provenance, secrets and recorded coursework deliverables. Read the README, implementation/integration/run guides, task board, existing AI record and relevant source/configuration/test entry points. Flagged historical ownership/status contradictions rather than asserting new assignments. The separately supplied course/contract PDFs were not inspected.

**Validation:** checked relative links, concrete file/command paths, Markdown fences and whitespace, plus `git diff --check`. This documentation change does not alter application code or models and did not require restarting Docker or rerunning live suites. Prepared on `docs/project-agent-context` for PR review.

## 2026-11-17 — documentation reconciliation and completion rule

**Tool:** OpenAI Codex. **Prompt summary:** fix the audited stale documentation and explicitly require agents to update documentation when major changes, features or milestones are completed.

**Output:** corrected the README, run guide, integration agreement, implementation guide and task board against current code and committed reports. Replaced the default-flow P2 receipt shortcut with P3 receipt/inspection, aligned demo actions and fresh-data prerequisites, separated verified local integration from pending external checks, updated current log references and split partially completed shared tasks. P4 ownership is explicitly awaiting confirmation because the earlier Abid attribution conflicts with the task board; no new owner was assigned. Added a mandatory cross-file documentation review and PR impact note to `AGENTS.md`. Preserved historical AI-use entries, model/log artifacts and verification results.

**Validation:** relative links/anchors, documented scripts, curl endpoint/payload shapes, Markdown fences and whitespace checked against repository files; `git diff --check` passed. Documentation only: no application/model edits or live-stack mutation. Extended documentation PR #7 with these corrections.

## 2026-11-04 to 2026-11-18 — P4 quality investigation in DCR (Abid Hasan)

**Tool:** Claude (Anthropic, Claude Opus 5.5) in a cloud workspace with a clone of the repository; no sub-agents.

**Prompt summary (paraphrase):** read the course material, proposal and repository; plan P4 and write its AS-IS/TO-BE, rules and DCR graph step by step with the owner; check the owner's dcrgraphs.net drawing and simulation; then align P4 with the merged P1–P3 code and finish it to the same standard (code, diagrams, compliance evidence, documentation) for a PR.

**Assistance:** generated `dcr/p4_quality_investigation.xml` from a reviewed rule list (the owner drew and simulated the same graph in dcrgraphs.net); wrote `deployer/quality.py` (consumer of P2's `DeliveryRiskIncident` handoff, temperature-alert intake, DCR-gated activities, decisions through `Registry.accept_quality_status`, background retries), `deployer/dcr_client.py` (dcrgraphs.net REST client and a local engine on the same graph), demo option 18, `stage_tests/test_p4.py`, `stage_tests/check_p4_investigation.py`, `compliance/p4/` (rules, builder, forbidden traces, simulation log transcribed from the owner's trace screenshots) and the documentation updates listed in the PR. The `dcr/` PDFs are the owner's dcrgraphs.net screenshot and the owner's AS-IS BPMN figure.

**Actual results:** the full offline suite (81 tests, including the P1–P3 suite unchanged) passes with Python 3.12.3 (and 3.13); the exhaustive marking search finds 3 425 reachable markings with no deadlock and no dead activity; dcrgraphs.net runs 1–3 replay as valid and accepting, run 4 as an intentionally unfinished case; all 9 synthetic forbidden traces are rejected. See `compliance/p4/verification.json`.

**Not done / limits:** no live stack run (Docker Hub was not reachable from the AI workspace), no dcrgraphs.net API execution (team credentials not used by the AI; export blocked on our subscription) and no ProcessDoctor check. Siddhi temperature/recovery patterns and the cooling-command bridge are not implemented; those inputs are manual/simulated. The owner must review and be able to explain the changes, run the live check and record its result.
