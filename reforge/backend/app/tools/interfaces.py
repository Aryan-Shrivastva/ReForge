"""Typed tool contracts; concrete implementations arrive in M1 and M2."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool


class Sandbox(Protocol):
    def run(
        self, cmd: list[str], phase: Literal["install", "run"], timeout: int
    ) -> ExecResult: ...


class RepositoryTools(Protocol):
    def list_tree(self, root: Path) -> list[str]: ...

    def read_file(self, path: Path) -> str: ...

    def search_code(self, root: Path, query: str) -> list[str]: ...


class GitTools(Protocol):
    def commit(self, workspace: Path, message: str) -> str: ...

    def diff(self, workspace: Path) -> str: ...

