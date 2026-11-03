#!/bin/sh
# Run by the installer as the Pi user. Only the fixed, root-owned image is formatted.
set -eu
root=/var/lib/biomerte-file-storage
image=$root/volume.ext4
mountpoint=/srv/biomertefiles
unit=srv-biomertefiles.mount
sudo install -d -m 0755 -o root -g root "$root"
if [ ! -e "$image" ]; then
    tmp=$(sudo mktemp "$root/volume.XXXXXXXX")
    trap 'sudo rm -f -- "$tmp"' EXIT
    sudo fallocate -l 2147483648 "$tmp"
    sudo /sbin/mkfs.ext4 -q -F -E nodiscard -m 0 -L biome-files "$tmp"
    sudo ln "$tmp" "$image"
    sudo rm -f "$tmp"
    trap - EXIT
fi
[ "$(sudo stat -c %s "$image")" = 2147483648 ] || { echo "Unexpected existing image size" >&2; exit 1; }
# Formatting a loop image can discard extents on some systems. Plain fallocate
# preserves existing bytes and backs any missing extents on an older image.
blocks=$(sudo stat -c %b "$image")
if [ "$((blocks * 512))" -lt 2147483648 ]; then
    sudo fallocate -l 2147483648 "$image"
    blocks=$(sudo stat -c %b "$image")
    [ "$((blocks * 512))" -ge 2147483648 ] || { echo "Volume is not fully allocated" >&2; exit 1; }
fi
sudo mkdir -p "$mountpoint"
if ! mountpoint -q "$mountpoint"; then
    [ -z "$(sudo ls -A "$mountpoint")" ] || { echo "Storage mount point is not empty" >&2; exit 1; }
fi
stage=$(mktemp)
trap 'rm -f "$stage"' EXIT
cat > "$stage" <<EOF
[Unit]
Description=BiomeRTE 2 GiB file storage volume
[Mount]
What=$image
Where=$mountpoint
Type=ext4
Options=loop,nodev,nosuid,noexec
EOF
dest=/etc/systemd/system/$unit
if [ -e "$dest" ]; then
    cmp -s "$stage" "$dest" || { echo "Existing mount unit differs" >&2; exit 1; }
else
    sudo install -m 0644 "$stage" "$dest"
fi
sudo systemctl daemon-reload
sudo systemctl start "$unit"
device=$(findmnt -n -o SOURCE --target "$mountpoint")
sudo /sbin/losetup -j "$image" | cut -d: -f1 | grep -Fxq "$device" || {
    echo "Unexpected filesystem at $mountpoint" >&2; exit 1;
}
