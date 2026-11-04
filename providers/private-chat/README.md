# Private LM Studio chat bridge

This provider is separate from the coding agent. It receives only `chat.private.exchange:1` ciphertext. The private worker retains only operation IDs, hashes and fence/status metadata on disk. Response retry ciphertext exists in RAM only until Biome acknowledges it. Interrupted requests fail without replaying inference. No plaintext is placed in the controller, tool logs or coding checkpoints.

## Install on Mac mini
Use a checkout of this repository and the existing authenticated Mac terminal transport.
```sh
python3 -m venv ~/.local/share/biome-private-chat-venv
~/.local/share/biome-private-chat-venv/bin/pip install -r providers/private-chat/requirements.txt
~/.local/share/biome-private-chat-venv/bin/python providers/private-chat/private_chat.py install
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.biomerte.private-chat.plist
```
The installer never prints the key. It stores it in macOS Keychain and writes a mode-0600 pairing.json at ~/.local/share/biome-private-chat/pairing.json for direct transfer to the phone. The pairing file is not a chat log. Remove it after transfer; the Keychain copy remains.

## Required LM Studio privacy qualification
Inference is disabled until verified. `store:false` prevents stateful API chat storage but does not by itself establish that server/inference logs omit prompts. Inspect this installed LM Studio version's logging configuration and ensure prompt/completion logging is disabled. Do not set the qualification marker based only on the request's store flag.

After testing with synthetic canary content and verifying that no request/response text is written to LM Studio logs/conversation storage, write this non-secret marker to ~/.local/share/biome-private-chat/privacy.json:
```json
{"lmstudio_request_logging_disabled":true,"qualification":"verified"}
```
The app must not be described as meeting the no-logs requirement until this is observed. The marker is an installation qualification, not automatic evidence; requalify after LM Studio changes. No existing LM Studio records are deleted by this installer.

No chat timeouts or CPU/memory limits are imposed. Core dumps are disabled in this bridge. OS swap/runtime copies and LM Studio's in-memory inference cache remain outside zeroization guarantees.

## Tests
Run `python -m unittest discover -s providers/private-chat -p 'test_*.py'` in a Python container with cryptography installed. Include cross-language direction-key/AES-GCM vectors and phone device lifecycle qualification.
