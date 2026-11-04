# Mac ComfyUI image capabilities

Two independent Biome contracts use the same generation engine and physical resource:
- `image.generate:1`: normal Biome image generation. Inputs: prompt; optional width, height, steps, seed, batch_size, cfg, guidance and negative_prompt. PNGs return as SHA-256-verified artifact chunks.
- `image.private.exchange:1`: Android encrypted image generation. Signed ephemeral X25519 handshakes, HKDF-SHA256 directional keys and AES-256-GCM. Large encrypted replies return ciphertext-only artifact chunks. Image handshakes use the distinct `biome-private-image` domain.

Model: Fluxed Up 7.1 GGUF Q8_0; CLIP-L; T5 XXL GGUF Q8_0; FLUX AE. Exact runtime revisions are in runtime-versions.json. Default generation is 512x512, 28 steps, DPM++ 2M / beta, FLUX guidance 3.5 and CFG 1. The engine accepts image parameters rather than arbitrary executable node graphs.

Both offers share one GPU admission slot. This is an initial advertised capacity, configurable in the provider; it avoids admitting simultaneous models into the same Mac's shared memory. There is no generation wall-clock deadline.

## Privacy and persistence
ComfyUI listens only on localhost. Its database is memory-only, previews and prompt metadata are disabled, and process output goes to /dev/null. The custom output node returns PNG bytes from RAM without saving images. Prompt history and image results are cleared from ComfyUI after collection; model caches are freed after each request. This is application-level retention behavior; Python and the operating system do not promise forensic RAM erasure or prevention of OS swap.

The provider ledger stores operation hashes, fencing and status, with no prompt/image payloads. Generic calls intentionally carry normal request parameters and PNG artifacts through Biome. Encrypted calls expose only public handshake metadata, ciphertext and artifact transport metadata to Biome. The Mac reuses the existing Private Chat signing identity and current paired phone's public key, with no persistent conversation encryption key. Never reset the phone pairing to run a test.

## Android
APK 0.3.0 adds Generate image to the existing app. It uses the existing pairing and OAuth connection. Image prompts/results/history are held in the password-encrypted phone vault and displayed only while unlocked. The image resource is derived from the paired privatechat.mac resource by replacing that prefix with image.mac. Chat behavior and its cryptographic domain remain intact.

## Install / operate
Install ComfyUI and its requirements in ~/Developer/biome-comfyui/venv; install city96/ComfyUI-GGUF in custom_nodes and its requirements. Download the four files in qualify_all.py and verify their hashes. The BiomeRTE checkout must include providers/private-chat and this directory. Run `venv/bin/python BiomeRTE/providers/private-image/private_image.py install`, then bootstrap the generated org.biomerte.comfyui and org.biomerte.private-image LaunchAgents. They start when the user's GUI session logs in, including the existing headless autologin configuration.

Use ordinary cap.acquire / cap.lease / cap.invoke / cap.status / cap.release with image.generate:1, or cap.run_async for completion via the existing agent continuation mechanism. Example inputs:
```json
{"prompt":"A mountain lake at sunrise","width":1024,"height":1024,"steps":28,"seed":12345}
```

Do not invoke raw image requests on image.private.exchange:1. Use image_client.py for reference handshake behavior; encrypted request plaintext has messages:[{role:"user",content:<prompt text or image parameter JSON>}]. Authenticate the Mac's pinned public identity before encryption. Retrieve ciphertext chunk manifests, verify SHA-256, decode the encrypted envelope JSON and authenticate/decrypt before accepting the image.

## Qualification
`python -m unittest -v test_private_image` exercises tampering, replay, domain separation, large encrypted reply chunking and generic artifact transport.
ImageInteropTest.java uses independently generated Python fixed vectors for Java/Python byte-for-byte interoperability.
`python qualify_all.py` waits for download completion, verifies all hashes and Q8_0 tensor types, then exercises real generic and encrypted Biome image generation. The encrypted live probe uses an isolated synthetic phone identity, never the owner's enrollment token, and removes its temporary capability offer.
Physical Android pairing, background/lock/resume and display testing remain separate from these automated checks.
