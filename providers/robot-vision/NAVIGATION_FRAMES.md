# Metric navigation-frame preparation

This pure Python layer supplies the first geometry prerequisite identified in
MAPPING_PREPARATION.md. It does not modify the live vision service or attach an
unmeasured transform to the robot. The external same-Mac brain can call:

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

A map revision or scale solution change invalidates the prior floor alignment.
Revalidate the alignment for the new geometry and publish a new attachment
revision rather than silently reusing old axes after loop corrections. This
initial layer deliberately supports only exact revision matches. A future
anchor resolver can preserve alignments across provably unchanged geometry.
A camera/mount change similarly needs a new mounting/calibration record.

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
