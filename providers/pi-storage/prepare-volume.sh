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
    sudo /sbin/mkfs.ext4 -q -F -m 0 -L biome-files "$tmp"
    sudo ln "$tmp" "$image"
    sudo rm -f "$tmp"
    trap - EXIT
fi
[ "$(sudo stat -c %s "$image")" = 2147483648 ] || { echo "Unexpected existing image size" >&2; exit 1; }
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
