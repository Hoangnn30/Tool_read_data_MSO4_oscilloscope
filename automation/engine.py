from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from instrumentation import DeviceRegistry


@dataclass(slots=True)
class TestStep:
    name: str
    device: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    save_as: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    delay_after_s: float = 0.0


@dataclass(slots=True)
class TestResult:
    step: str
    passed: bool
    value: Any = None
    error: str = ""
    elapsed_s: float = 0.0


class AutomationEngine:
    """Small stable test DSL.

    IC-specific values should be supplied through a profile and referenced by
    params. Device drivers stay unchanged when an IC changes.
    """

    def __init__(self, registry: DeviceRegistry):
        self.registry = registry
        self.values: dict[str, Any] = {}
        self.stop_requested = False
        self.on_step: Callable[[TestStep, TestResult], None] | None = None

    def request_stop(self) -> None:
        self.stop_requested = True

    def run(
        self,
        steps: list[TestStep],
        profile: dict[str, Any] | None = None,
    ) -> list[TestResult]:
        profile = profile or {}
        self.stop_requested = False
        results: list[TestResult] = []

        for step in steps:
            if self.stop_requested:
                break

            started = time.monotonic()
            try:
                value = self._execute(step, profile)
                passed = self._evaluate(value, step.minimum, step.maximum)
                result = TestResult(
                    step=step.name,
                    passed=passed,
                    value=value,
                    elapsed_s=time.monotonic() - started,
                )
            except Exception as exc:
                result = TestResult(
                    step=step.name,
                    passed=False,
                    error=str(exc),
                    elapsed_s=time.monotonic() - started,
                )

            if step.save_as and not result.error:
                self.values[step.save_as] = result.value

            results.append(result)

            if self.on_step:
                self.on_step(step, result)

            if step.delay_after_s > 0:
                time.sleep(step.delay_after_s)

        return results

    def _resolve(self, value: Any, profile: dict[str, Any]) -> Any:
        if isinstance(value, str) and value.startswith("$profile."):
            key = value.removeprefix("$profile.")
            return profile[key]
        if isinstance(value, str) and value.startswith("$value."):
            key = value.removeprefix("$value.")
            return self.values[key]
        return value

    def _execute(self, step: TestStep, profile: dict[str, Any]) -> Any:
        driver = self.registry.get_driver(step.device)
        if driver is None:
            raise RuntimeError(f"Device {step.device!r} is not connected.")

        params = {
            key: self._resolve(value, profile)
            for key, value in step.params.items()
        }

        action = step.action.lower().strip()

        if action == "write":
            driver.write(str(params["command"]))
            return None

        if action == "query":
            return driver.query(str(params["command"]))

        if action == "get_data":
            return driver.get_data(**params)

        if action == "start":
            driver.start()
            return None

        if action == "stop":
            driver.stop()
            return None

        if action == "sleep":
            time.sleep(float(params.get("seconds", 0)))
            return None

        raise ValueError(f"Unsupported automation action: {step.action}")

    @staticmethod
    def _evaluate(
        value: Any,
        minimum: float | None,
        maximum: float | None,
    ) -> bool:
        if minimum is None and maximum is None:
            return True

        numeric = float(value)

        if minimum is not None and numeric < minimum:
            return False
        if maximum is not None and numeric > maximum:
            return False
        return True
