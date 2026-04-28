"""Loader for task and model YAML files.

Lives separately from runner.py so analyze.py / tests can reuse it without
pulling in pentagi-invocation machinery. See bench/contracts.md for the
schemas this module validates.
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

BENCH_ROOT = Path(__file__).resolve().parent
TASKS_ROOT = BENCH_ROOT / "tasks"
MODELS_ROOT = BENCH_ROOT / "models"

# ---------------------------------------------------------------------------
# Dataclasses (a thin typed view over the raw dicts; raw stays accessible for
# pass-through fields W3/W4 may add later).
# ---------------------------------------------------------------------------


@dataclass
class SuccessCriterion:
    type: str
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SeedMemory:
    domain: str
    content: str
    confidence: float = 0.8


@dataclass
class Task:
    name: str
    description: str
    path: Path  # absolute path to task.yaml
    compose_file: Optional[Path]
    target_host: str
    success_criterion: SuccessCriterion
    prompt_template: Optional[str]
    seed_memories: List[SeedMemory]
    timeout_seconds: int
    tags: List[str]
    upstream_prompt: Optional[str]  # demo tasks only
    raw: Dict[str, Any]

    @property
    def task_id(self) -> str:
        """Stable identifier including the task group (demo/foo, synthetic/bar)."""
        rel = self.path.parent.relative_to(TASKS_ROOT)
        return rel.as_posix()


@dataclass
class Model:
    name: str
    provider: str
    path: Path
    base_url: Optional[str]
    env: Dict[str, str]
    pentagi_provider_config: Optional[str]
    flow_provider_name: str
    raw: Dict[str, Any]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value: str) -> str:
    """Resolve ${VAR} references from the runner's process environment.

    Unset variables are left as-is so the runner's --dry-run path can show
    which secrets a real run would need without requiring them to be present.
    """

    def repl(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1), match.group(0))

    return _ENV_REF.sub(repl, value)


def _expand_env_in_dict(d: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in d.items():
        if v is None:
            continue
        out[str(k)] = _expand_env(str(v))
    return out


def _require(raw: Dict[str, Any], key: str, context: str) -> Any:
    if key not in raw or raw[key] in (None, ""):
        raise ValueError(f"{context}: missing required key '{key}'")
    return raw[key]


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_task(yaml_path: Path) -> Task:
    yaml_path = yaml_path.resolve()
    raw = yaml.safe_load(yaml_path.read_text()) or {}
    ctx = f"task {yaml_path}"

    name = _require(raw, "name", ctx)
    description = raw.get("description", "")

    compose_file_raw = raw.get("compose_file")
    compose_file = (
        (yaml_path.parent / compose_file_raw).resolve() if compose_file_raw else None
    )

    target_host = raw.get("target_host", "")
    success_raw = raw.get("success_criterion") or {}
    if not success_raw and not raw.get("upstream_prompt"):
        # demo tasks may skip a strict criterion; everyone else needs one.
        raise ValueError(f"{ctx}: missing 'success_criterion' (or 'upstream_prompt')")
    sc_type = success_raw.get("type", "self_report")
    success = SuccessCriterion(type=sc_type, raw=success_raw)

    seed_memories: List[SeedMemory] = []
    for m in raw.get("seed_memories") or []:
        seed_memories.append(
            SeedMemory(
                domain=m.get("domain", "general"),
                content=m["content"],
                confidence=float(m.get("confidence", 0.8)),
            )
        )

    return Task(
        name=name,
        description=description,
        path=yaml_path,
        compose_file=compose_file,
        target_host=target_host,
        success_criterion=success,
        prompt_template=raw.get("prompt_template"),
        seed_memories=seed_memories,
        timeout_seconds=int(raw.get("timeout_seconds", 1800)),
        tags=list(raw.get("tags") or []),
        upstream_prompt=raw.get("upstream_prompt"),
        raw=raw,
    )


def load_model(yaml_path: Path) -> Model:
    yaml_path = yaml_path.resolve()
    raw = yaml.safe_load(yaml_path.read_text()) or {}
    ctx = f"model {yaml_path}"

    name = _require(raw, "name", ctx)
    provider = _require(raw, "provider", ctx)
    base_url = raw.get("base_url")
    env = _expand_env_in_dict(raw.get("env") or {})
    flow_provider_name = raw.get("flow_provider_name", name)
    pentagi_provider_config = raw.get("pentagi_provider_config")

    return Model(
        name=name,
        provider=provider,
        path=yaml_path,
        base_url=base_url,
        env=env,
        pentagi_provider_config=pentagi_provider_config,
        flow_provider_name=flow_provider_name,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_tasks(selectors: Optional[List[str]] = None) -> List[Task]:
    """Find every task.yaml under bench/tasks/ matching the selectors.

    Each selector is a path relative to bench/tasks/ — e.g.
    "demo/web-pentest-demo" matches exactly that task, "synthetic/*" matches
    every synthetic task, and None / [] matches everything.
    """
    if not TASKS_ROOT.exists():
        return []

    yaml_paths = sorted(TASKS_ROOT.glob("**/task.yaml"))
    tasks = [load_task(p) for p in yaml_paths]

    if not selectors:
        return tasks

    keep: List[Task] = []
    for t in tasks:
        rel = t.path.parent.relative_to(TASKS_ROOT).as_posix()
        for sel in selectors:
            # support glob-style selectors
            if glob.fnmatch.fnmatch(rel, sel) or rel == sel or t.name == sel:
                keep.append(t)
                break
    return keep


def discover_models(selectors: Optional[List[str]] = None) -> List[Model]:
    if not MODELS_ROOT.exists():
        return []

    # Exclude *.compose.yaml — those are docker-compose stacks for vLLM models,
    # not model profiles. They sit next to profile YAMLs by W4 convention.
    yaml_paths = [
        p for p in sorted(MODELS_ROOT.glob("*.yaml")) + sorted(MODELS_ROOT.glob("*.yml"))
        if not p.name.endswith(".compose.yaml") and not p.name.endswith(".compose.yml")
    ]
    models = [load_model(p) for p in yaml_paths]

    if not selectors:
        return models

    keep: List[Model] = []
    for m in models:
        for sel in selectors:
            if glob.fnmatch.fnmatch(m.name, sel) or m.name == sel:
                keep.append(m)
                break
    return keep
