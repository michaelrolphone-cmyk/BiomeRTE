# Exact resume of cleanup for publication 44648102932

This isolated branch prepares the reviewed resume of cleanup run 44654682862.
That run confirmed one deletion: service-runner ARMv7 version 1601148605 returned
HTTP 204. Its next attempt, ARM64 version 1601148152, returned HTTP 400 and stopped.
No other operation was attempted. The complete original inventory and that exact
partial receipt remain checked in, with both SHA-256 hashes pinned in the script.

The resume accepts only that history and the complete remaining inventory:
service-runner has its original ARM64 version, service-authority has both original
versions, and portable-services has both original versions. All package IDs,
owner/repository IDs, public visibility, creation times, version IDs, digests, tags
and original update timestamps must match. Any additional or missing version,
replacement package, or changed prior receipt blocks execution.

The workflow defaults to verify-only and remains at .github/workflows/ghcr.yml.
It permits only a manual run in michaelrolphone-cmyk/BiomeRTE on
work/ghcr-guarded-cleanup-20261120. Before selecting mode delete, all six exact
images must have proven private copies and concurrent original-package writers
must be stopped. These are root's execution prerequisites, not newly assumed
properties established by this script.

Delete mode verifies all three complete remaining inventories before any
mutation. Immediately before each deletion it rereads the package identity and
every remaining version. It then uses the documented whole-package DELETE for
that exact original package. No per-version delete is allowed by the client.
This is a specific reviewed resume, not an automatic fallback for HTTP 400 or any
other failure. Each DELETE must return HTTP 204; unexpected results stop without
retrying or attempting a different operation.

Receipts persist the immediate inventory before each request, the returned HTTP
status after the request, and the subsequent absence check. All original package
API entries must be absent at completion. Fresh anonymous authorization/manifest
checks cover every original digest and tag, including the already removed ARMv7
version. Network errors and unexpected statuses do not count as denial.

The original inventory comes from read-only run 44651896412, artifact 14591865623.
Its estimated start field was 00:49:45Z; actual observed creation began at
2026-11-20T00:49:44Z and ended at 00:50:17Z. The script verifies those exact observed
timestamps. The prior cleanup receipt identifies workflow commit
dd72877d2a33dac489e519ac0af563e75f5b2e9c. The new receipt and both original evidence
files are uploaded together; no image archive or credential is included.

GitHub does not document an atomic inventory-and-delete operation. Immediate
checks detect changes before the next deletion, while stopping concurrent writers
remains necessary. The script never invents a conditional-delete endpoint.

Only the workflow's own GITHUB_TOKEN is used, with package admin inherited from
the original publishing repository. Anonymous checks exclude this credential.
The script does not modify live containers, local images, private replacement
packages, credential configuration or package visibility.

Deletion cannot retract earlier downloads. Restoration is not automatic:
GitHub's 30-day restoration requires an available namespace, and public package
download limits can prevent deletion. Preserve the original publication lock,
qualified image evidence, private copies and cleanup history independently.

Primary documentation:
- https://docs.github.com/en/packages/learn-github-packages/deleting-and-restoring-a-package
- https://docs.github.com/en/rest/packages/packages
