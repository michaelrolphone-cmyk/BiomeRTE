# Biome Private Chat (Android 8+)

Native Android app and isolated Mac provider for encrypted conversations through the existing Biome capability transport.

## Build
Build the Dockerfile in this directory, then run its image with the repository mounted at /source:
`gradle -p android/private-chat --no-daemon testDebugUnitTest assembleDebug`.
Install app/build/outputs/apk/debug/app-debug.apk. The debug build is signed with a development key but has debugging disabled. Keep that signing key for updates; distribution signing is not yet configured.

## First use
1. Install and qualify the Mac bridge per providers/private-chat/README.md.
2. Create your phone passphrase (12+ characters; a long unique passphrase is recommended).
3. Tap Pair Mac / connection. Transfer the Mac pairing.json directly by USB or another trusted local method and select Import Mac pairing file. Do not send it through Biome, chat, email, or cloud storage. Delete the transfer copy afterward.
4. Tap Connect Biome. In the browser allow access, long-press “Continue to your app” and copy its link. Return to the app, unlock it, open connection settings, paste the link, and finish the connection. This uses the gateway's existing exact HTTPS redirect and PKCE support without changing its OAuth policy.
5. Send a message. Returning to another app locks this app immediately. Unlock and use Resume pending request to retrieve the same response.

Your password encrypts the phone vault, which includes phone history, pairing key and OAuth credentials. It is never transmitted. Forgetting it loses access to the vault. No recovery or biometric bypass is included. Automatic backup, screenshots, recents previews, UI state saving and personalized keyboard learning are disabled. A third-party keyboard can still observe input; use a trusted keyboard.

## Protocol and privacy
AES-256-GCM encrypts authenticated request/response envelopes before capability dispatch; Biome stores ciphertext and routing metadata. A 256-bit random pairing key lives in the Mac Keychain and the phone's password-encrypted vault. HMAC-SHA256 derives independent direction keys. AAD binds protocol version, direction and UUID operation ID. Every encryption uses a fresh 96-bit random nonce. Phone history uses an independent PBKDF2-HMAC-SHA256 key (600,000 iterations, random 256-bit salt), AES-GCM and atomic ciphertext-only writes.

The Mac worker ledger contains opaque operation IDs, hashes and fence/status metadata only. Responses are retained in RAM until Biome acknowledges them, then discarded; they are not stored in the Mac ledger. The adapter passes plaintext in memory to localhost LM Studio, with storage disabled and no coding tools/checkpoints. It sends encrypted responses to Biome. App/bridge code does not log conversation text. Existing LM Studio logging must be qualified before the bridge can advertise availability.

This is a shared pairing-key protocol, not a forward-secret ratchet. Compromise of that key permits decryption of retained Biome ciphertext. Neither Android nor Python guarantees zeroization of immutable strings, runtime copies, swap, model KV caches or OS diagnostic memory; a complete device-forensics guarantee is outside this app's control.

## Qualification status
Source is a first implementation. Do not treat it as security-audited. Device lock/unlock, OAuth browser return, Android backup exclusion, ciphertext-only persistence, LM Studio request logging and a full physical phone-to-Mac conversation require observed qualification before release.

## Observed checks (2026-11-03 America/Denver)
Android APK build, three Java cryptography tests and five Python envelope/retention tests passed in Mac Docker. Two synthetic LM Studio requests left no canary text in inspected storage; conversation and API-prediction-history stores did not change. Server metadata logging changed without canary text. Final physical phone lifecycle/OAuth/end-to-end qualification is pending.

Mac bridge installation is blocked: login Keychain returned -61 (write permissions), and macOS security reported user interaction is not allowed. The encryption key was not stored and the private provider is not enabled. Unlock the login keychain locally on the Mac, then rerun the installer and complete pairing; no password should be sent in chat.
