# P4 integration sequencing — 18 November 2026

**Current checkpoint:** PR #11 is merged as `f475904` after explicit user approval. Main was merged into the shared foundation branch in `4b09482`. The [handover](P4-FOUNDATION-HANDOVER.md) and contract fixtures are committed in `8d13c3e`; foundation PR #9 still requires separate review/merge approval and merged-main smoke. The restoration checks below describe the earlier revert checkpoint. The [18 November foundation review report](../compliance/foundation-review-18.json) records subsequent live verification and fresh offline reproduction of the three P4 contract failures; DCR calls were substituted in that reproduction.

Abid owns P4. His executable investigation, DCR models, code and historical evidence are preserved in [PR #10](https://github.com/ash-01xor/FrostFlow/pull/10), its head `45f654b` and merge `af3c68c1e98e6d3245cfdea9c7ade36bbae32a5d`. Removing that merge from the active tree defers integration; it does not discard his contribution or imply it never existed.

The agreed order is **revert PR → foundation PR #9 → verified merged main → replacement P4 PR**. Each merge requires a separate explicit user review approval. This revert PR is checkpoint 1 only. No foundation or P4 integration is claimed complete here.

## Why restore the baseline first?

Source review compares P4 at `af3c68c` with foundation `8726a497521cea312703ef31e219248349f81145` ([PR #9](https://github.com/ash-01xor/FrostFlow/pull/9)):

- `QualityService.accept_handoff` only accepts `DeliveryRiskDetected` with a `riskEvent`. Foundation assessment intake also requires temperature, combined-risk, telemetry-unavailable and evaluation-unavailable messages. Its assessment evidence cannot be handled by this delivery-risk-only parser.
- The P4 acknowledgement returns `handoff`, `incidentId` and `effect`, without the top-level matching `deliveryId` and `status: accepted` or `duplicate` required by the foundation sender. Durable acceptance must remain independent of DCR availability.
- `_apply_disposition` calls `accept_quality_status` without `expectedEvidenceVersion`. Foundation monitored shipments require the version actually assessed. The P4 conflict path also sets `dispositionApplied=True` after recording `StaleDispositionIgnored`; a stale decision needs reassessment, not a successful-application marker.
- Both branches define `P4_INCIDENT_URL` in the same Compose mapping: P4 defaults to its local consumer, foundation defaults to empty. Combining both entries would create a duplicate setting; the future resolution must retain exactly one empty-default setting until a compatible consumer exists. Neither branch alone has duplicate entries.
- Both branches allocate CLI option **18**: P4 investigation versus foundation temperature simulation. Keep temperature simulation at 18 and assign P4 investigation **19** in the replacement PR.

These are source-review findings in this turn. The supplied plan reports three earlier contract reproductions; those combined-branch experiments were not rerun here, and this record does not claim fresh reproduction or live integration results.

## Restoration and verification

The working tree was clean before fetching. `origin/main` was `af3c68c`; its first-parent pre-P4 baseline is `552945d`. The shared foundation branch was fast-forwarded from `88e27db` to `8726a49`, retaining Arunima’s P1/P2/P3 screenshots without rewriting history.

Branch `revert/p4-until-foundation` was created from `origin/main`. Commit `31ad23b` runs `git revert -m 1 af3c68c`. `git diff --exit-code 552945d 31ad23b` passes: all tracked files, including application behavior/configuration, match the pre-P4 baseline. The P4 consumer module, DCR client, blueprint/retry wiring, default consumer URL and investigation CLI additions are removed. The existing shared quality-status endpoint, durable hold and queued investigation handoff remain.

Checks on `31ad23b`, Python 3.12 in the existing `.venv`:

| Check | Outcome |
| --- | --- |
| `.venv/bin/python -m unittest discover -s stage_tests -p 'test_*.py' -v` | 57 tests pass |
| `docker compose config --quiet` | Pass |
| `git diff --check` | Pass |
| `git diff --exit-code 552945d 31ad23b` | Pass; exact tracked-tree restoration |

Intentional subsequent differences from `552945d` are documentation only: this record, confirmed Abid ownership, accurate deferral/history references, the task-board review checkpoint and the AI-use entry in README, AGENTS, run/implementation/integration guides and project notes. Existing execution logs and reports are left intact with their original provenance. No BPMN/PDF content changes, live runs, provider checks, ProcessDoctor checks, stack rebuilds, registry changes or volume changes were performed. The foundation's 79-test suite and full live acceptance are later checkpoints, not results of this restored baseline.

## Next checkpoints

After explicit revert-merge approval, merge updated main into the shared foundation branch. Resolve documentation and add `docs/P4-FOUNDATION-HANDOVER.md` plus producer-checked contract fixtures. Complete the planned offline and sequential fresh-dataset live checks before foundation review. Keep ProcessDoctor external checks separate and pending if unavailable. After approved foundation merge, rebuild and smoke-test its exact merged revision.

Abid should branch from that verified foundation, deliberately restore the original work (normally by reverting removal commit `31ad23b` on the new feature branch), resolve conflicts and adapt intake, durable acknowledgements, assessed evidence versions, reassessment, receipt/finalization and terminal rejection. Reopening the old merged-and-reverted branch alone will not restore P4. Do not weaken foundation safeguards or treat historical P4 evidence as combined verification. Cooling/recovery contracts and P5/P6 remain separate future work.

## Checkpoint 4 — replacement P4 PR

Foundation PR #9 is merged as `ae941cc`. Branch `feat/p4-foundation-integration` was created from it.

| Commit | Content |
| --- | --- |
| Restore | `git revert 31ad23b` (reapplies merge #10). Conflicts resolved: team documentation kept as on main, exactly one Compose `P4_INCIDENT_URL` entry, CLI **18** remains the temperature simulation and the P4 investigation moves to **19**. |
| Contract v1 | `deployer/quality.py` adapted to the [handover](P4-FOUNDATION-HANDOVER.md): `DeliveryRiskDetected` (legacy and v1) and `AssessmentRequested` intake, request persisted and matched to its foundation outbox message, durable `{deliveryId, status}` acknowledgement, assessed evidence version used as `expectedEvidenceVersion` before the DCR decision, refused dispositions recorded unapplied with reassessment required, one durable identity for transient retries, terminal rejection. The temperature-specific P4 route from PR #10 is removed. |
| Evidence | Live runner rewritten for monitored shipments; PR #10 live run `900821e9` moved to `compliance/p4/historical/`. |
| Documentation | P4 guide, README, AGENTS, run guide, agreement, implementation guide, task board, handover status note, AI-use record. |

The three findings above are resolved in source and covered by `stage_tests/test_p4.py`. The suite runs on new monitored shipments with real registry/monitoring code and substituted Zeebe calls. It covers:

- release after physical receipt during a hold;
- replace and return;
- newer-evidence reassessment with a refused stale decision;
- duplicate and conflicting delivery;
- DCR outage recovery;
- transient failures;
- the eight handover fixtures consumed in order;
- legacy delay messages.

`compliance/foundation/review-18/legacy-contract-reproduction.py` reproduces the failures of the historical revision `af3c68c` by design; it is not a check of this branch. The combined live run (`stage_tests/check_p4_investigation.py`), a dcrgraphs.net repository-mode run and ProcessDoctor are pending; results will be recorded in `compliance/p4/verification.json`. Review and explicit merge approval are required.
