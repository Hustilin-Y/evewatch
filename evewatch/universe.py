from __future__ import annotations

import json
import re
import urllib.request
import zipfile
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

UNIVERSE_VERSION = 1
_WORD_RE = re.compile(r"[\w'][\w'\-]*")
_URL_RE = re.compile(r"https?://\S+")
MAX_NAME_WORDS = 4


class UniverseError(Exception):
    pass


@dataclass(frozen=True)
class SystemInfo:
    id: int
    name: str
    security: float


class Universe:
    """Star-gate graph with case-insensitive system lookup in free text."""

    def __init__(self, systems: list[SystemInfo], gates: list[tuple[int, int]], build: int | None = None):
        self.build = build
        self.systems = {s.id: s for s in systems}
        self._by_name = {s.name.lower(): s.id for s in systems}
        self._adj: dict[int, set[int]] = {s.id: set() for s in systems}
        for a, b in gates:
            if a in self._adj and b in self._adj:
                self._adj[a].add(b)
                self._adj[b].add(a)
        self._max_words = max((len(n.split()) for n in self._by_name), default=1)
        self._max_words = min(self._max_words, MAX_NAME_WORDS)

    def __len__(self) -> int:
        return len(self.systems)

    def has(self, name: str) -> bool:
        return name.lower() in self._by_name

    def canonical(self, name: str) -> str | None:
        sid = self._by_name.get(name.strip().lower())
        return self.systems[sid].name if sid is not None else None

    def add_links(self, pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Adds extra edges (Ansiblex, bridges). Returns pairs that could not be resolved."""
        unknown = []
        for a, b in pairs:
            ia, ib = self._by_name.get(a.lower()), self._by_name.get(b.lower())
            if ia is None or ib is None:
                unknown.append((a, b))
                continue
            self._adj[ia].add(ib)
            self._adj[ib].add(ia)
        return unknown

    def distances(self, origin: str, max_jumps: int) -> dict[str, int]:
        start = self._by_name.get(origin.lower())
        if start is None:
            return {}
        seen = {start: 0}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            if seen[node] >= max_jumps:
                continue
            for nxt in self._adj[node]:
                if nxt not in seen:
                    seen[nxt] = seen[node] + 1
                    queue.append(nxt)
        return {self.systems[i].name: d for i, d in seen.items()}

    def jumps(self, a: str, b: str) -> int | None:
        ia, ib = self._by_name.get(a.lower()), self._by_name.get(b.lower())
        if ia is None or ib is None:
            return None
        if ia == ib:
            return 0
        seen = {ia}
        queue = deque([(ia, 0)])
        while queue:
            node, dist = queue.popleft()
            for nxt in self._adj[node]:
                if nxt == ib:
                    return dist + 1
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append((nxt, dist + 1))
        return None

    def find_systems(self, text: str, aliases: dict[str, str] | None = None, ignore: set[str] | None = None) -> list[str]:
        """System names mentioned in text, in order, longest match first. Trailing '*' of links is ignored."""
        words = _WORD_RE.findall(_URL_RE.sub(" ", text))
        lowered = [w.lower() for w in words]
        alias_map = {k.lower(): v for k, v in (aliases or {}).items()}
        ignored = ignore or set()
        found: list[str] = []
        i = 0
        while i < len(lowered):
            matched = False
            for n in range(min(self._max_words, len(lowered) - i), 0, -1):
                phrase = " ".join(lowered[i : i + n])
                name = None
                if phrase in alias_map:
                    name = self.canonical(alias_map[phrase])
                elif phrase in self._by_name:
                    name = self.systems[self._by_name[phrase]].name
                if name is not None:
                    if name.lower() not in ignored:
                        found.append(name)
                    i += n
                    matched = True
                    break
            if not matched:
                i += 1
        return found

    def to_json(self) -> dict:
        gates = sorted({(min(a, b), max(a, b)) for a, nbrs in self._adj.items() for b in nbrs})
        return {
            "version": UNIVERSE_VERSION,
            "build": self.build,
            "systems": [
                {"id": s.id, "name": s.name, "security": round(s.security, 3)}
                for s in sorted(self.systems.values(), key=lambda s: s.id)
            ],
            "gates": [list(g) for g in gates],
        }

    def save(self, path: str | Path) -> None:
        path = Path(path)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self.to_json(), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def from_json(cls, data: dict) -> "Universe":
        if data.get("version") != UNIVERSE_VERSION:
            raise UniverseError(f"unsupported universe.json version {data.get('version')!r}; run build-universe again")
        try:
            systems = [SystemInfo(int(s["id"]), str(s["name"]), float(s.get("security", 0.0))) for s in data["systems"]]
            gates = [(int(a), int(b)) for a, b in data["gates"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise UniverseError(f"universe.json is malformed: {exc}") from None
        return cls(systems, gates, data.get("build"))

    @classmethod
    def load(cls, path: str | Path) -> "Universe":
        path = Path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise UniverseError(f"{path} not found; run `python -m evewatch build-universe` first") from None
        except (OSError, json.JSONDecodeError) as exc:
            raise UniverseError(f"cannot read {path}: {exc}") from None
        return cls.from_json(data)


SDE_LATEST_URL = "https://developers.eveonline.com/static-data/eve-online-static-data-latest-jsonl.zip"
SDE_USER_AGENT = "EveWatch/0.1 (one-off static data build)"


def _jsonl(archive: zipfile.ZipFile, name: str):
    try:
        handle = archive.open(name)
    except KeyError:
        raise UniverseError(f"{name} not found in the archive; the SDE layout may have changed") from None
    with handle:
        for raw in handle:
            if raw.strip():
                yield json.loads(raw)


def universe_from_sde_zip(path: str | Path) -> Universe:
    """Builds the graph from CCP's JSONL static data export (mapSolarSystems + mapStargates)."""
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise UniverseError(f"cannot open {path}: {exc}") from None
    with archive:
        build = None
        if "_sde.jsonl" in archive.namelist():
            build = next(iter(_jsonl(archive, "_sde.jsonl")), {}).get("buildNumber")
        systems = []
        for row in _jsonl(archive, "mapSolarSystems.jsonl"):
            try:
                name = row["name"]["en"]
                systems.append(SystemInfo(int(row["_key"]), name, float(row.get("securityStatus", 0.0))))
            except (KeyError, TypeError, ValueError) as exc:
                raise UniverseError(f"unexpected mapSolarSystems row format: {exc}") from None
        gates = []
        for row in _jsonl(archive, "mapStargates.jsonl"):
            try:
                gates.append((int(row["solarSystemID"]), int(row["destination"]["solarSystemID"])))
            except (KeyError, TypeError, ValueError) as exc:
                raise UniverseError(f"unexpected mapStargates row format: {exc}") from None
    if not systems or not gates:
        raise UniverseError("the archive contains no systems or no stargates")
    return Universe(systems, gates, build)


def download_sde(url: str, dest: Path, progress: Callable[[int, int | None], None] | None = None) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": SDE_USER_AGENT})
    tmp = dest.with_name(dest.name + ".part")
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(tmp, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0) or None
            done = 0
            while chunk := response.read(1024 * 256):
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        tmp.replace(dest)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise UniverseError(f"download failed: {exc}") from None
