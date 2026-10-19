"""Download the pinned inputs listed in sources.toml into a data directory."""

import gzip
import hashlib
import shutil
import tomllib
import urllib.request
import zipfile
from pathlib import Path


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch(sources, data_dir, only=None):
    with open(sources, "rb") as f:
        entries = tomllib.load(f)
    data_dir = Path(data_dir)
    for name, e in entries.items():
        if only and name not in only:
            continue
        dest = data_dir / e["dest"]
        if not (dest.exists() and sha256(dest) == e["sha256"]):
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            print(f"{name}: downloading {e['url']}")
            with urllib.request.urlopen(e["url"]) as r, open(tmp, "wb") as out:
                # some archives send the file gzipped whatever the request asked for
                shutil.copyfileobj(gzip.GzipFile(fileobj=r) if r.headers.get("Content-Encoding") == "gzip" else r, out)
            got = sha256(tmp)
            if got != e["sha256"]:
                tmp.unlink()
                raise SystemExit(f"{name}: SHA-256 mismatch ({got}); refusing it")
            tmp.replace(dest)
        else:
            print(f"{name}: ok")
        for member, target in e.get("extract", {}).items():
            target = data_dir / target
            if target.exists():
                continue
            with zipfile.ZipFile(dest) as z:
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(member) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
            print(f"  extracted {member}")
