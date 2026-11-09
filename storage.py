"""Job directories retained until platform upload has had time to finish."""

import json
import shutil
import tempfile
import time
import zipfile
from pathlib import Path


class JobStore:
    def __init__(self, root: Path, ttl_minutes: int):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_minutes * 60
        self.active: set[Path] = set()

    def create(self) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="job_", dir=self.root)).resolve()
        self.active.add(directory)
        return directory

    def finish(self, directory: Path):
        if directory in self.active:
            directory.touch()
            self.active.discard(directory)

    def clean(self):
        cutoff = time.time() - self.ttl_seconds
        for directory in self.root.glob("job_*"):
            # Avoid following symlinks or junctions when pruning plugin-owned jobs.
            if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
                continue
            if directory.resolve().parent != self.root or directory in self.active:
                continue
            try:
                if directory.is_dir() and directory.stat().st_mtime < cutoff:
                    shutil.rmtree(directory)
            except OSError:
                continue


def write_metadata(directory: Path, work: dict, text: str):
    (directory / "artwork.json").write_text(
        json.dumps(work, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (directory / "README.txt").write_text(text + "\n", encoding="utf-8")


def pack_zip(directory: Path, pid: str, images: list[Path], text: str, work: dict) -> Path:
    write_metadata(directory, work, text)
    target = directory / f"pixiv_{pid}.zip"
    # Images are already compressed; ZIP_STORED avoids wasting CPU and preserves bytes.
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for path in images + [directory / "artwork.json", directory / "README.txt"]:
            archive.write(path, arcname=path.name)
    return target
