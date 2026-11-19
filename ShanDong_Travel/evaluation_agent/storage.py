import json
import re
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class RunStore:
    def __init__(self, output: Path, task_id: str, demo: bool):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.root = output / f"{task_id}_{stamp}_{uuid4().hex[:6]}"
        self.root.mkdir(parents=True, exist_ok=False)
        (self.root / "raw_sources").mkdir()
        self.demo = demo
        self.events = []

    def save(self, name: str, value) -> None:
        write_json(self.root / name, value)

    def raw_text(self, source_id: str, text: str) -> str:
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", source_id) + "_" + hashlib.sha256(source_id.encode()).hexdigest()[:12]
        relative = f"raw_sources/{safe_id}.txt"
        (self.root / relative).write_text(text, encoding="utf-8")
        return relative

    def log(self, tool: str, status: str, **metadata) -> None:
        # Do not put API keys, authorization headers, or raw exception messages here.
        self.events.append({"time": now(), "tool": tool, "status": status, **metadata})
        self.save("tool_log.json", self.events)
        if tool == "llm":
            print(f"[模型] {metadata.get('stage', '')}：{status}（调用{metadata.get('call', '')}）",
                  file=sys.stderr, flush=True)
