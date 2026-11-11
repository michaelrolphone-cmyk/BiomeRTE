# Metric navigation-frame preparation

This layer supplies the first geometry prerequisite identified in
MAPPING_PREPARATION.md. The vision service exposes an additive robot_localization
object in coherent snapshots and geometry reads. Existing raw camera and metric
camera fields retain their semantics. No physical transforms are supplied by
default. The external same-Mac brain can also call the pure composition directly:

```python
from navigation_frames import derive_robot_pose
pose = derive_robot_pose(snapshot, floor_alignment, camera_robot_extrinsics,
                         delivery_elapsed_s=measured_snapshot_delivery_seconds)
```

The input snapshot is the direct snapshot object inside
robot.vision.snapshot's response. It includes metric_localization and freshness.
All pose and metric fields must refer to the exact same observation. Delivery
elapsed time comes from the caller's monotonic clock around its local snapshot
read. The output is an observation at that decoded frame, not a moving-vehicle
control permission.

## Transform directions and units

T_A_B maps coordinates in B into A. Every transform is rigid and translations
are in metres. Metric scale is already applied to T_map_metric_camera; it must
not be applied again:

```text
T_floor_robot = T_floor_map_metric * T_map_metric_camera * T_camera_robot
```

Robot axes are X forward, Y left, Z up. Camera optical axes are X right, Y down,
Z forward. Floor Z points up; the floor XY orientation is established by the
qualified alignment, not by an absolute compass. Robot origin must be selected
and measured consistently with its motion model. Camera offsets are rotated
with the camera, so a lever arm changes the derived robot position when turning.

Two separate attachment dictionaries are required:

| Field | Floor alignment | Camera/robot extrinsics |
| --- | --- | --- |
| schema_version | integer 1 | integer 1 |
| kind | map_floor_alignment | camera_robot_extrinsics |
| attachment_id, revision | stable nonempty ID; positive integer revision | same |
| units | m | m |
| from_frame | map_metric | robot |
| to_frame | floor | camera_optical |
| transform | measured T_floor_map_metric matrix | measured T_camera_robot matrix |
| qualification | status=qualified, nonempty method and evidence list | same |
| calibration_id | exact camera calibration ID | exact camera calibration ID |
| map_id, map_revision | exact qualified map and nonnegative integer revision | not required |
| metric_attachment_id, metric_solution_id | exact qualified metric solution | not required |
| physical_mount_id, robot_model_id | not required | exact physical mounting and robot-model identities |

No identity matrix is supplied as a physical default. Fixture matrices in tests
are mathematical examples, not this robot's measurements. Qualification records
are supplied evidence: this module validates their structure/bindings and
performs geometry; acquisition and physical qualification remain separate.

A scale solution change invalidates the prior floor alignment. A changed map
revision requires either explicit requalification or the unchanged-anchor proof
described below. Corrected/deleted/recreated reference keyframes cannot silently
retain a qualification. A camera/mount change similarly needs a new
mounting/calibration record.

## Output and interpretation

Unavailable inputs return valid=false, T_floor_robot=null and a specific reason.
A valid output contains the composed matrix, translation_m, floor-relative
heading_rad, observation identity and exact attachment revisions. A nearly
vertical robot-forward axis yields no floor heading. Reflections, scaling,
nonfinite matrices, stale responses, unqualified attachments and mismatched
map/calibration/metric identities are rejected.

navigation_available remains false: this layer supplies a pose, not
traversability, obstacle clearance or a feedback controller. Joint uncertainty,
velocity and occupancy stay null. Camera exposure timing remains unqualified;
fresh receive/decode evidence is not proof of a synchronized physical exposure.
Raw AHRS or radio availability never substitutes for metric visual geometry.

Run the dedicated tests:

```sh
python3 -S -m unittest discover -s providers/robot-vision/tests -p test_navigation_frames.py -v
```

They check noncommuting rotation/translation order, camera optical conventions,
mount lever arms, metric scale not being applied twice, exact revision/solution
bindings, stale transport, invalid matrices and independent input preservation.
No physical transform, hardware accuracy or robot motion is qualified here.

## Vision service integration

Write STATE/navigation-frames.json atomically with this top-level structure:

```json
{
  "schema_version": 1,
  "floor_alignment": {},
  "camera_robot_extrinsics": {}
}
```

The two attachment objects must contain the complete qualified fields above;
the empty objects shown here are intentionally unavailable, not physical
defaults. The optional file is read for each on-demand snapshot/geometry query.
It is bounded to 64 KiB and rejects duplicate fields and nonfinite JSON values.
No service restart is needed to replace a qualified attachment revision.
A missing/unreadable/malformed document leaves the original vision observation
available and reports an explicit unavailable robot_localization reason.
Derived poses are not written back into historical observations.

The service evaluates freshness at query time. Consumers must still account
for response-delivery time before using the observation. A stale frame or a
frame from a prior observation session never yields a current derived pose.
Without supporting native keyframes, the attachment requires its exact map
revision. To preserve alignment across unrelated map updates, include
supporting_keyframes in the qualified floor record. Supply 1–16 unique records,
each with keyframe_id, source_frame_id and its exact native T_map_camera at the
record's qualified map_revision. These native translations retain arbitrary
SLAM units; the floor transform itself remains in metres.

For a later snapshot revision, the service looks up each supporting keyframe
at both revisions using indexed entity-version queries. The source image and
native matrix must match the qualified record, and the current entity must be
the exact same entity-version row. New landmarks or semantic labels can then
advance the map without invalidating the floor. Any reference-row update,
deletion or replacement withholds the derived pose, even if a later pose equals
the original. Scale and calibration identities must still match.

The output includes the source/resolved revisions and native anchor-version
proof. The configuration file and original qualification revision are never
rewritten by a read. No history-wide JSON scan or guessed revision range is
used. Physical qualification owns which supporting keyframes established the
floor reference; the resolver does not infer that reference from arbitrary
landmarks.

Fixture integration tests exercise the actual vision service and retained
SQLite observation path using injected camera/detector/SLAM fixtures:
```sh
python3 -m unittest discover -s providers/robot-vision/tests -p test_navigation_service.py -v
```
