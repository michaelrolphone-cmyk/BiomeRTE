# Biome Decisions

Native Android decisions inbox and home-screen widget, backed solely by Biome. Package: `org.biomerte.decisions`. See [the decision and authorization protocol](../../docs/decisions-authorization.md).

The original Decisions widget layouts, colors and drawables were ported from the user's Capabilities Service Android app. The Biome app is a separate install and does not modify that app's pairing or data.

Build using JDK17 / Gradle8.9 / SDK35:

```
gradle testDebugUnitTest assembleDebug
```

Use **Choose pairing JSON** to import a `biome-decisions-pairing/1` bundle generated with `decision.pair`. Add **Biome Decisions** from Android's widget picker. Keep notifications enabled for the live decision inbox. Its foreground connection can be stopped from Android's active-app controls; saved responses remain durable and are retried when the app reconnects.

No Firebase or external messaging service is used. The user-enabled foreground service polls the signed Biome inbox every ten seconds; only one sync runs at once. This polling interval is a delivery tradeoff, not a deadline on decisions or workflow execution. Android may defer or stop background work under its own power policy. Locked phones hide question content and cannot answer.

## Inbox and reply recovery

Widget **Next**, **Back to inbox**, and local review remain available while the
network is waiting. Refreshes share one network worker; repeated taps do not
enqueue parallel syncs. The inbox refreshes before delivery attempts for saved
replies. A permanently rejected reply is retained with its exact original signed
envelope and request ID, marked **Reply retained · needs attention**, and excluded
from automatic retries. **Retry saved reply** is an explicit retry of that same
reply; it never changes a choice, rewrites text, or signs a replacement.

A signed written response that the service records as `needs_review` remains
**Reply retained · review required**. Text such as "ok" is not converted into an
approval. A fresh binary request remains independently answerable. To approve
without conditions, leave the optional reply empty and select its explicit
**Approve** option.

Malformed inbox entries stay in the encrypted cache for diagnosis, with response
controls disabled. They do not prevent valid entries from being reviewed.
Repeated pagination cursors stop refresh with an error and preserve the previous
inbox. An HTTP 403 is reported as a rejected request/reply; it is not assumed to
mean that the phone's key has changed.

The Android CI workflow runs the existing Gradle unit tests and builds review
APKs using this directory's Dockerfile. Its unsigned release APK must be signed
with the installed application's existing signing key for an in-place update.
The CI debug APK uses the runner's debug key and is for review/testing; it is not
assumed to be upgrade-compatible with an existing phone installation. Keep
application ID `org.biomerte.decisions`, pairing data, and Android Keystore keys.
Do not uninstall or clear app storage as a routine recovery step.
