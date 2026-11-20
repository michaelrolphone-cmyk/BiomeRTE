# One-time cleanup of publication 44648102932

This isolated branch prepares cleanup of only the three packages first published
from BiomeRTE commit a81497cf0465e980585cf72562c5bef059b3c7b7. It does not modify
running containers, local images, private replacement packages, credentials or
package visibility.

The workflow defaults to verify-only. A repository administrator must review the
exact branch head and select mode delete to execute the cleanup. Only a manual
run in michaelrolphone-cmyk/BiomeRTE on work/ghcr-guarded-cleanup-20261120 is allowed.
The normal publishing guards must already be in place and other publication
writers must be stopped before deletion.

The checked-in inventory is the exact successful read-only inventory artifact
from run 44651896412, artifact 14591865623. Its SHA-256 is pinned in the script.
The inventory's estimated start field was 00:49:45Z; observed version creation
began one second earlier. The cleanup verifies the actual observed interval
2026-11-20T00:49:44Z through 00:50:17Z and exact recorded creation/update times.

Before any deletion, all three package identities and complete version lists
must match. The guards require package IDs, owner ID/login, repository ID/name,
public visibility, container type, creation times, and all six exact version
IDs/digests/tag arrays. Each deletion is rechecked against the remaining complete
inventory, and a direct version read is matched immediately before its DELETE.
Only the six recorded version IDs may be removed. An empty package is deleted
only after a fresh same-ID check and empty complete inventory. If GitHub already
removed the empty package, its absence is recorded through verification.

Every attempted deletion is written to a receipt before the request; each
successful response must be HTTP 204. Partial failure stops immediately and
requires inspection. There are no automatic retries or restoration actions.
After deletion, all package API entries must be absent, and fresh anonymous
authorization/manifest checks must deny both the six digests and six tags.
Network errors and unexpected statuses do not count as proof of denial.

GitHub's APIs do not provide a documented atomic inventory-and-delete operation.
The checks detect changed or extra versions before subsequent deletions, but
stopping concurrent writers is still required. The script does not invent a
conditional-delete endpoint.

The workflow uses only its own GITHUB_TOKEN, with package admin inherited from
the original publishing repository. It never exports that token. Anonymous
registry checks omit this credential entirely. Artifacts contain metadata and
receipts only; no image archive or credential is uploaded.

Deletion does not retract earlier downloads. Restore is not automatic: GitHub's
30-day restoration requires an available namespace, and public package download
limits can prevent deletion. Preserve the original release lock, archive receipts
and copied replacement images independently.

Primary documentation:
- https://docs.github.com/en/packages/learn-github-packages/deleting-and-restoring-a-package
- https://docs.github.com/en/rest/packages/packages
