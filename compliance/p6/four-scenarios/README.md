Four live supplier-claim scenarios passed using the local stack built for d2d7c6f. The runner reused the two original claim IDs and exercised two additional incidents. The invalid outcome was refused before settlement (400); an attempt to reverse a closed rejection was refused (409). All four traces passed local replay with the revised P6 rules from the integration worktree. The two refused requests have no business activity in the XES; their live assertions are recorded in verification.json. The previous ProcessDoctor screenshot applies only to the two-trace file. External verification of this four-trace file is pending.

Reproduce on four suitable distinct incidents (use a fresh third incident for the pre-settlement guard):

python stage_tests/check_p6_claims.py SHIPMENT1 SHIPMENT2 --extra-shipments SHIPMENT3 SHIPMENT4 --output-dir runtime/verification-results/p6-four
