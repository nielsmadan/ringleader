import pytest
from ringleader.portability import Adapter, AdapterRegistry, PreservationError
from ringleader.portability.capabilities import (
    Compatibility,
    FormatProfile,
    NativeFile,
    NativeRoots,
    NativeStage,
)
from ringleader.portability.model import Identity

from .fixtures import NATIVE_PROFILES, claude_records, codex_records, opencode_export, pi_records


def test_registry_aliases_and_capabilities_are_explicit():
    registry = AdapterRegistry()
    adapter = Adapter("claude-code")
    registry.register(adapter)
    assert registry.get("claude") is adapter
    with pytest.raises(PreservationError, match="duplicate"):
        registry.register(adapter)
    with pytest.raises(PreservationError, match="Deep reading"):
        registry.reader("claude")
    with pytest.raises(PreservationError, match="Native writing"):
        registry.writer("claude")
    with pytest.raises(PreservationError, match="Native launch"):
        registry.launcher("claude")
    with pytest.raises(PreservationError, match="No portability adapter"):
        registry.get("unknown")


def test_source_profile_does_not_require_runtime_version():
    profile = FormatProfile("pi", "pi-v3", "3", (NATIVE_PROFILES["pi"],))
    assert Compatibility(True, profile, ()).require() == profile
    with pytest.raises(PreservationError, match="Unknown database schema"):
        Compatibility(False, None, ("Unknown database schema",)).require()


def test_staging_carries_files_and_schema_gated_database_payload(tmp_path):
    roots = NativeRoots(
        tmp_path, tmp_path / "store", tmp_path / "cwd", tmp_path / "config", tmp_path / "cache"
    )
    profile = FormatProfile("opencode", "v1", "1", (NATIVE_PROFILES["opencode"],))
    with pytest.raises(PreservationError, match="schema identity"):
        NativeStage(Identity("opencode", "new"), profile, roots, (), "new", b"graph")
    stage = NativeStage(
        Identity("opencode", "new"),
        profile,
        roots,
        (NativeFile("attachments/image.png", b"image"),),
        "new",
        b"graph",
        "verified-schema",
    )
    assert stage.files[0].data == b"image"
    assert stage.database_payload == b"graph"
    for name in ("../escape", "/absolute", ""):
        with pytest.raises(PreservationError, match="safe relative"):
            NativeFile(name, b"data")


def test_native_builders_express_agent_specific_envelopes(tmp_path):
    messages = [{"role": "user", "content": [{"type": "text", "text": "fixture"}]}]
    assert claude_records(tmp_path, messages)[0]["parentUuid"] is None
    assert pi_records(tmp_path, messages)[0]["version"] == 3
    assert (
        codex_records(
            tmp_path,
            [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "fixture"}],
                }
            ],
        )[0]["payload"]["source"]
        == "cli"
    )
    assert (
        opencode_export(tmp_path, [{"info": {"id": "msg_fixture", "role": "user"}, "parts": []}])[
            "messages"
        ][0]["info"]["id"]
        == "msg_fixture"
    )
