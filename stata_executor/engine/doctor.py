from __future__ import annotations

from stata_executor.contract import DoctorResult, Edition
from stata_executor.runtime import RuntimeBootstrapError, resolve_configuration
from stata_executor.runtime.executable_resolver import resolve_stata_executable


def build_doctor_result(
    *,
    stata_executable: str | None = None,
    edition: Edition | None = None,
) -> DoctorResult:
    try:
        resolved = resolve_configuration(
            stata_executable=stata_executable,
            edition=edition,
        )
    except RuntimeBootstrapError as exc:
        return DoctorResult(ready=False, errors=[str(exc)])

    if resolved.stata_executable is None:
        return DoctorResult(
            ready=False,
            errors=["Missing explicit 'stata_executable' input."],
        )

    executable = resolve_stata_executable(resolved.stata_executable, resolved.edition)
    if executable is None:
        return DoctorResult(
            ready=False,
            errors=[
                f"Path does not resolve to a usable Stata executable: {resolved.stata_executable}"
            ],
        )

    return DoctorResult(ready=True, errors=[])
