# Biome Private Chat 0.3.0

Android 8+ app and isolated Mac LM Studio provider. Keychain is not used.

## Keys and storage
| Material | Phone | Mac |
|---|---|---|
| Password-derived vault key | Memory while unlocked | Never received |
| Phone signing identity | Password-encrypted vault | Public key only |
| Mac signing identity | Pinned public key in encrypted vault | Owner-only identity.ed25519 file; authenticates handshakes only |
| Ephemeral X25519/session keys | Pending state encrypted in vault; removed after reply | RAM only; never written to ledger |
| Conversation history | Password-encrypted vault | Never saved by bridge |

The Mac signing key is not a chat encryption key. Keeping it permits authenticated reconnection after Mac restarts without re-pairing. It is protected by file permissions, not Keychain or application-level file encryption.

## Protocol v2
Every message/reply exchange gets fresh X25519 keys, HKDF-SHA256 directional traffic keys and AES-256-GCM. The phone signs its client hello with Ed25519; the Mac signs a transcript binding both public ephemeral keys, both nonces, both identities and session/request identifiers. The phone verifies the Mac signature against its directly paired public key before dispatching encrypted conversation data.

Initial enrollment uses a random one-use 256-bit token transferred directly with the pinned Mac public key. The token is sent only inside authenticated session encryption. The Mac retains a token hash until enrollment, then replaces it with the phone's public signing key and deletes the Mac pairing file. Subsequent exchanges authenticate the paired phone without sending an enrollment token.

Sessions are consumed once. Replayed ciphertext cannot rerun inference. Mac restarts lose incomplete sessions; completed encrypted replies remain retrievable through Biome using the phone's encrypted pending state. Interrupted operations fail without automatic inference replay.

This is a project-specific handshake using maintained cryptographic implementations, not an implementation of HPKE or Noise. Test vectors cover Java/Python interoperability; physical phone and independent security review remain separate qualification.

## Build and install
Build the Dockerfile in this directory, then run its image with the repository mounted at /source:
`gradle -p android/private-chat --no-daemon testDebugUnitTest assembleDebug`.
The APK is app/build/outputs/apk/debug/app-debug.apk. It uses the retained development signing key with debugging disabled. Keep that signing volume for updates.

1. Install the updated APK. Existing password-encrypted history and Biome OAuth credentials remain compatible.
2. Transfer ~/.local/share/biome-private-chat/pairing.json directly from the Mac to the phone, such as by USB. Do not send this enrollment file through Biome or chat.
3. Unlock the app, select Pair Mac / connection, and Import Mac pairing file. Returning from the file picker requires unlocking again.
4. If not already connected, select Connect Biome; allow access in the browser, copy the Continue link, return and unlock, then paste the link to finish OAuth.
5. Send a message. Biome receives public handshake metadata and encrypted message envelopes only. Use Resume pending request after an interruption.

## Privacy
History, phone signing key, OAuth credentials and pending transport state share the password-encrypted vault. The passphrase never leaves the phone. The vault uses PBKDF2-HMAC-SHA256 (600,000 iterations, 256-bit random salt), AES-GCM and atomic ciphertext-only writes. Automatic backup, screenshots/recents previews, UI state saving and keyboard personalized learning are disabled. A third-party keyboard still sees input.

The Mac ledger stores only operation hashes, fencing/status metadata and generic failures. Request/response ciphertext retries live in RAM until Biome acknowledges them. Plaintext passes only in memory to localhost LM Studio, with store:false, no coding integrations and runtime guards requiring sensitive/incoming-token logging disabled. LM Studio retention must be qualified on the installed version.

No artificial inference timeouts or CPU/memory limits are imposed. Core dumps are disabled for the bridge. Runtime copies, OS swap, model KV caches and device compromise are outside a no-forensic-remnants guarantee.

## Encrypted image generation
Generate image uses the separate image.private.exchange:1 capability backed by Fluxed Up GGUF Q8_0 on the Mac. Image prompts and PNG replies are encrypted before dispatch and before leaving the Mac; image history stays in the password-encrypted phone vault. Existing chat, OAuth and pairing remain compatible. Images display only while unlocked and are cleared from the view on lock. Generic Biome callers separately use image.generate:1. Physical phone lock/resume/display qualification remains pending until tested on the device.
