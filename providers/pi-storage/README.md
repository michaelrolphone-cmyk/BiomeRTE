# Raspberry Pi file storage

Run `python3 providers/pi-storage/install.py` as the Pi account that owns the controller. The installer reserves a **2 GiB (2,147,483,648-byte)** image with `fallocate`, formats it once, mounts it at `/srv/biomertefiles` and enables `biomerte-file-storage.service`. Filesystem metadata, a 16 MiB transaction reserve, and the file index reduce the usable payload space. The service depends on the mount and checks the volume identity. It uses its own signing key. It needs Python 3.4+, SQLite, systemd, util-linux, and e2fsprogs.

## Operations

All contract names start with `storage.file.` and end with `:1`. Acquire the desired contract and invoke it with a unique operation ID. Read the result with `cap.status`. Release the lease before acquiring a different contract on the same volume. The file handle persists independently of the lease, including across service restarts. All callers with access to these storage offers share the same file namespace.

| Operation | Inputs | Result |
|---|---|---|
| open | path, mode | Handle, initial byte offset, file size |
| read | handle, optional length and offset | Base64 data, byte count, next offset, EOF |
| write | handle, base64 data, optional offset | Bytes written, next offset, file size |
| close | handle | Invalidates the handle |
| delete | path | Deletes a closed file |
| rename | path, to | Moves a file or directory. Existing handles follow the file |
| exists | path | Boolean and type when present |
| size | path | File length in bytes |
| list | path, optional limit and after | Immediate entries and a continuation name, or null |
| mkdir | path | Creates a directory in an existing parent |
| rmdir | path | Removes an empty directory |

Use `r` to read, `w` to create or truncate, `a` to append, or `x` to create exclusively. Append `+` for read/write. Paths are relative to the volume root, with or without a leading slash. Directory traversal and replacement of an existing rename destination are rejected. Reads and writes accept up to 65,536 bytes per call, and default to the current handle position. Writes may overwrite existing bytes or extend from EOF. Append mode always writes at EOF. Listing returns up to 200 names per page, sorted by name.

Check the provider's `ok` field before consuming `result`. Expected failures have an `error` code, such as `NOT_FOUND`, `FILE_OPEN` or `NO_SPACE`. Each operation commits its data and handle position in one transaction. Deleted file blocks are reclaimed in small batches to keep a full volume usable. The controller's own command history is stored separately.

## Move to a larger disk

Mount the new filesystem at a persistent mount point (for example `/mnt/biomestorage`) and configure it to mount at boot. Stop the storage service, copy the complete volume data directory, then run the same installer with the new mount.

```sh
sudo systemctl stop biomerte-file-storage.service
sudo cp -a /srv/biomertefiles/biomerte-files /mnt/biomestorage/
python3 providers/pi-storage/install.py --existing-volume /mnt/biomestorage
```

The installer checks the copied database's volume identity and uses the new mount's capacity. The signing key, resource ID and worker ledger stay in `~/.local/share/biomerte-file-storage`. Retain the original volume until you verify a read from the new one.
