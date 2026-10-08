"""Module node — groups a set of connections under a named subsystem."""

from dataclasses import dataclass, field
from typing import List

from .connection import Connection


@dataclass
class Module:
    name: str
    description: str
    connections: List[Connection] = field(default_factory=list)

    def add(self, conn: Connection) -> None:
        self.connections.append(conn)

    def by_id(self, conn_id: int) -> Connection:
        for c in self.connections:
            if c.id == conn_id:
                return c
        raise KeyError(f"Connection {conn_id} not in module {self.name}")

    def ids(self) -> List[int]:
        return [c.id for c in self.connections]
