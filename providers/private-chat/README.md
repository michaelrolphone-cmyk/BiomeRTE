# Private LM Studio provider, protocol v2

No Keychain dependency. The only persistent private key is an owner-only Ed25519 signing identity at ~/.local/share/biome-private-chat/identity.ed25519. It authenticates the Mac; it does not encrypt conversations. X25519 private/session keys exist only in RAM and are discarded after each request/reply.

## Install
```sh
python3 -m venv ~/.local/share/biome-private-chat-venv
~/.local/share/biome-private-chat-venv/bin/pip install -r providers/private-chat/requirements.txt
~/.local/share/biome-private-chat-venv/bin/python providers/private-chat/private_chat.py install
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.biomerte.private-chat.plist
```
The existing Mac Biome OAuth transport remains the controller connection. Source checkout must remain available at the installed path.

Transfer the mode-0600 pairing.json directly to the phone. It contains the Mac public identity, provider resource and one-use enrollment token. The token itself is not persisted in the manifest; only its hash is. After successful phone enrollment, the Mac records the phone public key, removes the enrollment hash and deletes its pairing file. Delete the phone's transfer copy after import.

To replace a lost phone, explicitly run install --reset-pairing and restart this provider. This revokes the old phone and creates a new enrollment file while preserving the Mac identity. Never perform a reset while a real request is in progress.

## Logging qualification
Inference remains unavailable until ~/.local/share/biome-private-chat/privacy.json contains:
```json
{"lmstudio_request_logging_disabled":true,"qualification":"verified"}
```
Write this marker only after synthetic canary testing confirms that LM Studio does not retain prompt/completion text and does not change its chat/API-prediction history. Each inference also checks installed logSensitiveData=false and logIncomingTokens=false. store:false alone is insufficient evidence.

## Durable behavior
worker-v2.sqlite contains operation hashes, fences and status only. The protocol handles request plaintext in process; no subprocess spool or chat checkpoint exists. Responses awaiting controller acknowledgment are RAM-only ciphertext. After a crash, a previously started request fails rather than replaying inference. Completed encrypted responses are retrievable from Biome, not from a Mac transcript log.

Tests run in Docker with cryptography installed and the repository root on PYTHONPATH. make_vectors.py emits explicitly synthetic interoperability fixtures. session_client.py is a reference test/qualification client; it is not a substitute for physical Android testing.
