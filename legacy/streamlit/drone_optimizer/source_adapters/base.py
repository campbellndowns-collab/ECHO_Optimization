from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable

@dataclass
class AdapterStatus:
    name: str
    ok: bool
    records: int = 0
    optimizer_eligible: int = 0
    message: str = ""

class SourceAdapter(ABC):
    name: str
    component_types: tuple[str, ...]

    @abstractmethod
    def fetch(self):
        raise NotImplementedError

    @abstractmethod
    def normalize(self) -> Iterable[dict]:
        raise NotImplementedError

    @abstractmethod
    def status(self) -> AdapterStatus:
        raise NotImplementedError
