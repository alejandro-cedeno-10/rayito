# Sigue https://docs.e2b.dev/sandbox/metadata y https://docs.e2b.dev/sandbox/list.
import uuid

from rayito.e2b import Sandbox, SandboxQuery, SandboxState

run = uuid.uuid4().hex
sbx = Sandbox.create(metadata={"corpus": "info_list", "run": run})
try:
    info = sbx.get_info()
    assert info.sandbox_id == sbx.sandbox_id
    assert info.sandbox_domain, info
    assert info.template_id and info.name, info
    assert info.metadata == {"corpus": "info_list", "run": run}, info.metadata
    assert info.state is SandboxState.RUNNING, info.state
    assert info.end_at is not None and info.end_at > info.started_at, info
    assert info.cpu_count is not None and info.cpu_count >= 1, info
    assert info.memory_mb is not None and info.memory_mb > 0, info
    assert info.envd_version, info
    assert info.allow_internet_access is not False, info
    assert info.network is None or set(info.network) == {"allow_out", "deny_out"}, info
    assert info.lifecycle is not None and info.lifecycle["on_timeout"] == "kill", info
    assert info.volume_mounts == [], info

    paginator = Sandbox.list(query=SandboxQuery(metadata={"run": run}), limit=10)
    found: list[str] = []
    while True:
        found.extend(item.sandbox_id for item in paginator.next_items())
        if not paginator.has_next:
            break
    assert found == [sbx.sandbox_id], found
finally:
    sbx.kill()
print("info_list ok")
