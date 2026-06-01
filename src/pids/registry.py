"""Registry de PIDs. SRP: carrega YAML e provê lookup.
OCP: adicionar PID novo = editar YAML, não código."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import yaml

from .decoders import DECODER_MAP

import importlib.resources
import io

@dataclass(frozen=True)
class PidDefinition:
    code: str          # "010C"
    name: str          # "engine_rpm"
    description: str
    unit: str
    priority: int      # 1=alta, 2=média, 3=baixa
    decoder: Callable[[str], Optional[float]]

    @property
    def mode(self) -> str:
        return self.code[:2]

    @property
    def pid(self) -> str:
        return self.code[2:]

    @property
    def response_prefix(self) -> str:
        """Para 010C → '410C'"""
        mode_resp = format(int(self.mode, 16) + 0x40, "02X")
        return mode_resp + self.pid


class PidRegistry:
    def __init__(self, yaml_path: str | Path):
        self._by_code: dict[str, PidDefinition] = {}
        self._load(Path(yaml_path))

    def _load(self, path: Path) -> None:
        
        raw_data = None
        
        # Estratégia 1: Tentar carregar como arquivo local (Desenvolvimento)
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                raw_data = f.read()
        else:
            # Estratégia 2: Tentar carregar como recurso embutido (Android/Build)
            try:
                # Assume que o yaml está em src.config
                pkg = "src.config"
                resource = importlib.resources.files(pkg).joinpath(path.name)
                raw_data = resource.read_text(encoding="utf-8")
            except Exception as e:
                raise FileNotFoundError(f"Não foi possível carregar o PIDs YAML: {e}")

        data = yaml.safe_load(io.StringIO(raw_data))
        
        for entry in data.get("pids", []):
            decoder_name = entry["decoder"]
            if decoder_name not in DECODER_MAP:
                raise ValueError(
                    f"Decoder '{decoder_name}' não registrado em DECODER_MAP"
                )
            pid_def = PidDefinition(
                code=entry["code"].upper(),
                name=entry["name"],
                description=entry["description"],
                unit=entry["unit"],
                priority=int(entry["priority"]),
                decoder=DECODER_MAP[decoder_name],
            )
            self._by_code[pid_def.code] = pid_def

    def get(self, code: str) -> Optional[PidDefinition]:
        return self._by_code.get(code.upper())

    def all(self) -> list[PidDefinition]:
        return list(self._by_code.values())

    def by_priority(self, priority: int) -> list[PidDefinition]:
        return [p for p in self._by_code.values() if p.priority == priority]


def extract_payload(cleaned_response: str, pid_def: PidDefinition) -> Optional[str]:
    """Extrai payload pós '41XX' da resposta limpa.
    Aceita também respostas multi-frame (ISO) que vêm com header."""
    prefix = pid_def.response_prefix
    match = re.search(prefix, cleaned_response)
    if not match:
        return None
    return cleaned_response[match.end():]
