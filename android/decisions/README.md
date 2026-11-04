# Biome Decisions

Native Android decisions inbox and home-screen widget, backed solely by Biome. Package: `org.biomerte.decisions`. See [the decision and authorization protocol](../../docs/decisions-authorization.md).

The original Decisions widget layouts, colors and drawables were ported from the user's Capabilities Service Android app. The Biome app is a separate install and does not modify that app's pairing or data.

Build using JDK17 / Gradle8.9 / SDK35:

```
gradle testDebugUnitTest assembleDebug
```

Use **Choose pairing JSON** to import a `biome-decisions-pairing/1` bundle generated with `decision.pair`. Add **Biome Decisions** from Android's widget picker. Keep notifications enabled for the live decision inbox. Its foreground connection can be stopped from Android's active-app controls; saved responses remain durable and are retried when the app reconnects.

No Firebase or external messaging service is used. The user-enabled foreground service polls the signed Biome inbox every ten seconds; only one sync runs at once. This polling interval is a delivery tradeoff, not a deadline on decisions or workflow execution. Android may defer or stop background work under its own power policy. Locked phones hide question content and cannot answer.
