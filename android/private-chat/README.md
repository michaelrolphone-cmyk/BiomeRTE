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


## Conversation controls (0.8.0)
The conversation selector creates and switches threads; each thread retains its own
messages, images, draft and selected model inside the existing encrypted vault.
Existing history migrates into Conversation 1 without changing pairing or OAuth.
Rename the current thread from its selector or the conversation menu.

Use the menu on a message to delete it. Image cards start collapsed; Show/Hide
controls persist their visibility. Delete removes the whole result, or expand a
multi-image result to delete an individual image. Deletion affects local encrypted
history and future inference context; an already dispatched request can still
contain the earlier message. Image positions are adjusted when earlier text is deleted.

Switching threads parks an in-flight request for explicit resumption. Every saved
request retains its originating thread; its result is added there even if resumed
from another thread. Terminal failures are retained in Pending requests; starting
one again requires an explicit action because a previous lost result may have
already run. Transport interruptions continue to resume the same operation.
Assistant model labels remain display metadata and are excluded from chat context.

Image generation progress is authenticated and encrypted by the Mac using the
image session. The app displays preparation, sampling step/total, decoding and
delivery updates; prompts and image bytes are excluded from progress records.
Progress failures do not cancel or repeat generation.

## Named threads, instructions and pins (0.9.0)
Open Conversation settings from the menu or conversation selector to edit a
thread's name, multiline instructions and pin. Pinned threads appear first;
pinning does not change thread identity or request ownership.
Instructions are stored only in the existing encrypted vault. For new chat
requests they are prepended as a system message; image requests carry them inside
the encrypted request and include them in the image prompt while preserving
explicit image parameters. Each request snapshots its instruction context when
created. Editing a thread never changes an already-started saved request.

Conversations now has a dedicated inbox screen rather than a dropdown selector.
It opens after unlocking and is reachable with Back / All conversations from a
chat. Rows show names, pins, draft or recent-message previews, and thread settings.
Search filters names/previews locally while unlocked. Opening a row restores its
history and draft; pinning affects only list order.

0.10.0: Saved chat/image requests resume automatically after unlock and foreground.
An encrypted request queue stores each original operation ID, transcript and keys.
Each outstanding request has an independent network client and watcher; navigating
threads does not stop it. The Mac continues queued work while Android is closed;
reopening collects encrypted results into the originating thread, once, at the
saved prompt anchor. Network interruptions reconnect automatically. Explicitly
failed operations remain visible for a deliberate restart, to avoid duplicate work.
Model rows merge exact IDs and loaded flags; distinct quantizations stay separate.

0.11.0: Tap the info button on an assistant response for its requested model,
resolved loaded instance and LM Studio-reported model. A verified label requires
all authenticated provenance fields to agree. Older responses show their saved
label with verification unavailable; the selected model is never substituted
for a missing actual model. Inspect the immutable instructions/messages sent for
that response, queue/model preparation and inference timing, token counts, finish
reason and request/session IDs. Internal model templates/truncation are not
captured. Metadata and context live only inside the encrypted phone vault and
are projected out of future inference messages. Deleting a message removes saved
context snapshots in that thread and redacts pending response snapshots. Info
windows are secure and close when the vault locks.

0.12.0: Delete conversation is available from the thread row menu and the chat
menu. Confirmation removes the complete encrypted thread, its queued request
records and local watchers. Other conversations and their in-flight requests
continue. Late replies cannot restore a deleted thread. The inbox remains empty
when its last thread is deleted. A Mac operation already running may finish;
its deleted thread's result is ignored.

Copy buttons on messages copy exact prompt/response text. Image results include
Copy prompt and Copy image while collapsed; batches offer image selection and
expanded images have per-image long-press actions. Images use PNG content URIs
through a private, read-only provider with temporary Android clipboard grants.
Temporary image bytes are encrypted at rest with an Android Keystore key and
streamed to authorized paste consumers. Copying marks the clipboard sensitive
and does not add image/text data to inference context. Image paste requires a
recipient app with image clipboard support.
