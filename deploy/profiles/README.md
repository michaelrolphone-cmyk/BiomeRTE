# Mac Rover release source

The running Mac directory named `authority-safe-4915104c03b6` was audited on
2026-11-11. Its Python modules matched commit `4502c3ba21ee349a7fb61a5c2250a2850b9f0692`
except `biomerte/store.py` and `biomerte/assistant_support.py`. Both exceptions
matched commit `bcef0dad6f8c745d01603de11919d64547c665ad` exactly.

The Rover clock repair preserves those production versions. The profile pins
each exception and its SHA-256; it does not change the repository's newer
storage implementation. Updating these storage pins is separate work requiring
storage qualification.

Build this runtime with `deploy/build_rover_release.py REPOSITORY RELEASE_COMMIT TARGET`.
Use the exact pushed commit containing the profile and an absent target path.
The builder reads committed Git objects, ignores mutable checkout contents,
checks override hashes, and writes a complete Python source hash manifest.
Keep that manifest with the running release and audit it before replacing it.
Do not deploy a mutable checkout or overwrite this profiled runtime with an
unqualified plain archive. The adapter commit and firmware commit are recorded
in the profile; camera-light and world-map modules remain in the adapter source.
