"""Build NoCloud ``cidata`` ISOs in memory; free of HTTP code so tests load it."""

from __future__ import annotations

import io

import pycdlib

VOLUME_LABEL = "CIDATA"


def _iso9660_name(name: str, index: int) -> str:
    """An 8.3 ISO9660 level-1 name; Rock Ridge and Joliet carry the real one."""
    return f"/F{index:07d}.;1"


def build_seed_iso(files: dict[str, bytes]) -> bytes:
    """Return an ISO labelled CIDATA holding ``files`` (flat, real names via Rock Ridge/Joliet).

    cloud-init's NoCloud datasource reads ``user-data`` and ``meta-data`` from it;
    any other file (the sandbox bundle) is read by the user-data script itself.
    """
    if "user-data" not in files or "meta-data" not in files:
        raise ValueError("a cidata ISO needs user-data and meta-data")
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident=VOLUME_LABEL)
    for index, (name, content) in enumerate(sorted(files.items())):
        if "/" in name or not name:
            raise ValueError(f"seed files are flat: {name!r}")
        iso.add_fp(
            io.BytesIO(content),
            len(content),
            _iso9660_name(name, index),
            rr_name=name,
            joliet_path=f"/{name}",
        )
    out = io.BytesIO()
    iso.write_fp(out)
    iso.close()
    return out.getvalue()


def meta_data(instance_id: str, hostname: str) -> bytes:
    return f"instance-id: {instance_id}\nlocal-hostname: {hostname}\n".encode()
