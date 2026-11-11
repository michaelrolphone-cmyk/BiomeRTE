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
