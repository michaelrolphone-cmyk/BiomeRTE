# Continuous environment mapping

The world identity survives process restarts, checkpoint saves and local tracking loss.
Native map IDs are submap identities, not replacement environments.

A lost camera first attempts native visual relocalization without deleting the initialized
map or additions made since checkpoint load. Five distinct textured recovery observations
with failed localization allow the background world worker to checkpoint the prior submap
and initialize local mapping in an unknown area. A dark or featureless frame never triggers
this transition. The old geometry and checkpoint remain intact.

New local coordinates are explicitly unlocalized. They are joined to the world only after
mutual distinctive ORB correspondences pass 3D Sim(3) RANSAC: at least 20 inliers, 70% support,
a 2%-of-scene-extent residual bound and observations from at least 3 keyframes on both sides.
Collinear, repetitive, contradictory or insufficient matches remain unlocalized.
Local optimization invalidates affected links until refitted against current checkpoint
geometry. The world pose remains unavailable for an unlocalized submap.

Read robot.world.read:1 for the persistent environment. It returns the same
paged map/entities envelope, namespaced source IDs, verified world coordinates, and a
separate unlocalized_submaps list. A normal read remains the native active submap for
existing coherent camera/map consumers. World cursors pin source revisions and transforms
and are integrity-protected. Unknown submaps can be read using their map_id in native
coordinates; they must not be drawn over the world origin.

world.sqlite3 stores membership, transforms and match evidence only. It does not store
images, native maps or copies of checkpoint packages. Existing current-and-rollback
checkpoint retention stays in effect. Map geometry continues to follow native validity and
bundle adjustment; an append-only collection of rejected landmarks is not a reconstruction.

Startup imports existing map/checkpoint identities into the world index. This retains old
disconnected mapping attempts without pretending they share a coordinate frame. Historical
map association does not establish geometric alignment; that still requires verified matches.

## Offline video producer handoff

Producer imports `WorldImportQueue` from `world_import.py`. The single running
atlas owner consumes this durable queue on its background thread. Do not create
a second WorldAtlas writer: its landmark cache is process-local.

1. Decode/reconstruct in an isolated backend with validated video calibration.
   Keep reconstruction, cancellation and media outside the active rover session.
2. Only after successful reconstruction, use VisionStore at
   `<vision_state>/observations` to create ONE map with video provenance and its
   calibration ID. Apply the final native keyframe/sparse-point changes with
   `apply_map_update(map_id, expected_revision, changes)`. Do not open/activate it
   through VisionService, update runtime.json, or reset the rover backend.
3. Prepare/finalize ONE matching backend checkpoint using
   `prepare_checkpoint(map_id, metadata={..., "geometry_matches_backend_state": True})`
   and `finish_checkpoint(prepared, backend_artifact=<native package path>)`.
   The artifact must be only the isolated native SLAM package, never a parent
   observations/state/checkpoint directory. Preserve source PTS/frame IDs.
4. After the checkpoint is durable, submit exact immutable references:

   ```python
   from world_import import WorldImportQueue
   queue = WorldImportQueue(vision_state)
   queue.submit(import_id, map_id, checkpoint_id)
   receipt = queue.get(import_id)
   ```

An identical retry returns the same durable operation. Reusing import_id for a
new map/checkpoint fails. Poll get(): queued -> integrating -> completed/failed.
The completion receipt reports world_id, world_revision, map_id, checkpoint_id,
and alignment. Completed with alignment=unlocalized is a successful retained
import, not a failure. Alignment can improve later; consult world status then.
The owner rejects the active rover map and missing/superseded checkpoints.
A restart retries an integrating reference without creating another map/package.
The producer must persist and reuse its map/checkpoint IDs across job retries;
queue idempotency does not deduplicate maps a producer independently creates.
Cancel reconstruction before final publication. Once handed off, keep the
finished submap; cancellation must not erase accumulated world geometry.

The queue stores small references/receipts in world-imports.sqlite3, no images,
geometry or checkpoint clones. Frame delivery does not wait for import fitting.
Upload, job scheduling and UI are owned by the video-import worker.
