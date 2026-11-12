# Generic capability contracts and request placement

The catalog has one versioned contract for each operation, independent of machines and implementations.
Each contract lists its current implementations with provider, resource, advertised execution host,
admission state and capacity. The offers view remains available for implementation prerequisites and diagnostics.
An admitted offer is eligible; capacity and lease fencing still determine when it can execute.

Use the same host.terminal.run:1 contract to run a portable network check on any eligible host
or inspect memory on a particular machine. Host-specific shell syntax remains the caller's responsibility.

Automatic execution:

    cap.submit {contract:"host.terminal.run:1", operation:"network-check-unique",
                inputs:{argv:["/bin/bash","-c","ping -c 1 google.com"]}}

Host-specific execution:

    cap.submit {contract:"host.terminal.run:1", operation:"memory-check-unique",
                placement:{host:"<advertised Pi hostname>"},
                inputs:{argv:["/bin/bash","-c","cat /proc/meminfo"]}}

cap.acquire, cap.submit and cap.prepare accept optional placement fields provider, host and resource.
All supplied fields must match. The existing top-level resource field remains supported;
placement.resource is an equivalent alias, and conflicting aliases are rejected.
Provider/resource IDs match exactly. Hostnames compare case insensitively, ignoring a final dot.
Hostnames are advertised metadata, never inferred from Mac/Pi words or opaque resource IDs.
A host constraint cannot match an implementation without host metadata.
Generic native terminal and Docker workers advertise execution_host on registration.

Omitting placement and resource lets the controller select an admitted implementation with capacity.
An explicit constraint stays queued if unavailable; enqueue:false on cap.acquire returns an error instead.
No constrained call silently falls back to another machine. A workspace, storage volume or device
requires its particular resource; a matching execution host does not imply interchangeable local data.

Constraints persist with queued leases and combined submissions across restarts.
An existing operation retains its selected lease/provider and fence. A retry that changes placement
or inputs is rejected rather than launching duplicate work. Unknown execution outcomes must be reconciled.
Releasing a queued request cancels it normally. Existing callers and legacy lease identities remain valid.

cap.run_async accepts the same placement object for the worker; caller_resource still selects
the callback receiver independently. BPMN capability service tasks support b:placementRef
for a bound object or b:placement for a JSON object. Existing running workflows keep their
published graph revision; newly requested placement upgrades an older follow-up definition.
