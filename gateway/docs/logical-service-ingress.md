# Logical service ingress

SERVICE_AUTHORITY_ORIGIN points to the HTTPS coordinator origin. SERVICE_AUTHORITY_DECLARATIONS is a JSON array of objects with service_id, authority, and providers fields. Example with placeholder identities:

[{"service_id":"relay-authority","authority":"<logical 64-hex ID>","providers":["<provider 64-hex ID>"]}]

These declarations enroll a provider in a logical service; they do not choose a host. The same declaration remains valid when Docker ownership moves between controllers. Install the declaration during the initial scoped custody handoff, after source admission is durably frozen and before publishing the activation manifest. The first request retains service scope metadata in D1. Neither payloads nor ownership grants are stored there.

Existing native controller identities and unenrolled providers keep their original routes. Enrolled services return retryable unavailability while their witness or active authority is unavailable; they never silently revert to native ownership. Original signed request bytes are forwarded unchanged. Calls already pinned to native dispatch origins retain those origins.

Removing an environment declaration does not remove retained scope metadata. Do not disable SERVICE_AUTHORITY_ORIGIN to reverse a completed custody handoff; reconcile service ownership and source state explicitly. New services may use the same Docker deployment and adapter protocol; enroll any provider that requires this public ingress with its logical authority identity.
