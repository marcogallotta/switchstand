from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class ProviderTaskReference:
    provider: str
    provider_work_id: str


def asana_task_id(value: str) -> str:
    if value.isdecimal():
        return value
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.netloc != "app.asana.com":
        raise ValueError("expected an Asana task ID or https://app.asana.com task URL")
    parts = [part for part in parsed.path.split("/") if part]
    if "task" in parts:
        index = parts.index("task") + 1
        if index < len(parts) and parts[index].isdecimal():
            return parts[index]
    if len(parts) >= 3 and parts[-1] == "f" and parts[-2].isdecimal():
        return parts[-2]
    if len(parts) >= 3 and parts[0] == "0" and parts[-1].isdecimal():
        return parts[-1]
    raise ValueError("Asana URL does not contain a task ID")


def parse_legacy_task_reference(value: str) -> ProviderTaskReference:
    return ProviderTaskReference(provider="asana", provider_work_id=asana_task_id(value))
