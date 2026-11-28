# Recorded video import

The rover dialog accepts an existing recording or invokes the phone recorder through its native file picker. Select the calibration matching the recording camera and lens. Uploads without calibration remain available until a measured profile is selected or supplied as JSON.

Uploads are acknowledged in resumable 256 KiB chunks; an identical retransmission does not append bytes twice. Re-select the same filename/size and choose the existing import to resume. Stable job/map/checkpoint identities are retained for retries. There is no total video size or execution-duration cutoff; actual available disk and native-worker capacity apply.

Reconstruction runs in a separate process and session, so closing the browser or restarting the rover bridge does not cancel it. A process file lock distinguishes a live worker from an interrupted worker. Native mapping jobs serialize on an explicit resource lock to avoid launching multiple large SLAM engines; camera/video/control sockets remain independent.

Recorded media PTS and calibrated encoded dimensions feed the isolated backend. Its source media digest and frame indices remain separate from native frame indices. All final entities are read from the actual saved native map, then one submap and one checkpoint are published through VisionStore and WorldImportQueue. No live calibration, camera, active-map pointer or runtime.json is written. No second WorldAtlas writer is created.

Cancel is available before publication. After publication starts, the accumulated submap is retained. A successful unlocalized import is explicitly labelled awaiting world alignment. Only the existing atlas owner establishes verified world coordinates. Source media and the duplicate private native package are removed after durable queue submission.

Tests cover resumable/conflicting upload ranges, incomplete upload, calibration prerequisites, cancellation, process custody, failed reconstruction isolation, idempotent checkpoint handoff, real PyAV decoding, and authenticated HTTP tunnel chunks. Full source identity and release qualification are recorded in Rhizome task b4615cfc-c0ff-4aad-9ed3-8b4e368ab57e.
