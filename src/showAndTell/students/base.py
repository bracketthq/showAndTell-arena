"""Extension contract shared by every ShowAndTell student adapter."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from showAndTell.core import chrome


@dataclass
class Session:
    """Everything one teach/trial run carries between adapter hooks."""

    mode: str
    out: object
    task: object
    source_dir: Path
    question_count: int
    subject_name: str = ""
    run_dir: Path | None = None
    cdp_port: int = 9223
    codex_cdp_port: int = 9333
    profile_root: Path = chrome.MANAGED_ROOT
    timeout_minutes: int = 20
    manual: bool = False
    wait_done: object = input
    capture: dict | None = None
    manual_script: Callable[[Path], list[str]] | None = None
    narration_routes: dict[str, Callable] = field(default_factory=dict)
    stack: ExitStack | None = None
    audio: ExitStack | None = None
    pw: object = None
    ctx: object = None
    page: object = None
    runtime: object = None
    app_url: str = ""
    credentials: dict = field(default_factory=dict)
    result: dict | None = None

    def connect_chrome(self):
        """Attach to the managed Chrome and cache its browser context."""
        self.ctx = self.pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self.cdp_port}").contexts[0]
        return self.ctx

    def demo_script(self) -> list[str]:
        """Return runner-provided teleprompter lines without importing it."""
        if self.manual_script is None:
            raise RuntimeError("the teacher did not provide a demonstration script")
        return self.manual_script(self.source_dir)

    def narration(self, route: str):
        """Build a teacher-provided narration context by stable route name."""
        try:
            factory = self.narration_routes[route]
        except KeyError:
            raise RuntimeError(f"narration route {route!r} is unavailable") from None
        return factory(out=self.out)


class ProductAdapter:
    """Hooks implemented by systems that learn a demonstrated workflow."""

    name = ""
    display = ""
    teach_label = ""

    def preflight(self, session: Session) -> None:
        """Check run requirements before launches, seeding, or artifacts.

        Runs in teach and trial mode alike; gate mode-specific checks on
        ``session.mode``.
        """

    def launch_steps(self, session: Session) -> list:
        raise NotImplementedError

    def arm_steps(self, session: Session) -> list:
        raise NotImplementedError

    def demo_label(self, session: Session) -> str:
        if session.mode == "trial":
            return "replaying only the recorded task actions…"
        return "performing the demonstration (narrating aloud)…"

    @contextmanager
    def demo_stage(self, session: Session):
        yield session.page

    def demo_kwargs(self, session: Session) -> dict:
        return {}

    def manual_teach(self, session: Session) -> None:
        raise RuntimeError(f"{self.name} teach has no manual mode")

    def conclude_steps(self, session: Session) -> list:
        raise NotImplementedError

    def ask(self, session: Session, message: str) -> str:
        raise NotImplementedError

    def quiz_label(self, session: Session) -> str:
        return (f"quizzing {self.display} ({session.question_count} questions) "
                "to judge understanding…")

    def quiz_intro(self, session: Session) -> str | None:
        return None

    def save_artifacts(self, session: Session) -> None:
        """Persist product-specific artifacts after the graded result."""

    def trial_metadata(self, session: Session) -> dict:
        return {}
