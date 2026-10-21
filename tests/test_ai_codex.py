from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from email_reader.ai import AssistantService, OpenAIService, OperationCancelled
from email_reader.config import AppSettings
from email_reader.contracts import DraftCandidate, DraftSet
from email_reader.database import Database
from email_reader.security import MemoryVault
from email_reader.usage import UsageLedger


class ExampleResult(BaseModel):
    value: str


def test_api_has_bounded_timeout_without_hidden_retries(database, monkeypatch):
    captured = {}
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: captured.update(kwargs) or object())
    vault = MemoryVault()
    vault.set("openai-api-key", "fictional-test-key")
    service = OpenAIService(vault, AppSettings(ai_backend="api"), UsageLedger(database, 10))
    service._client()
    assert captured["timeout"] == 30.0
    assert captured["max_retries"] == 0


def test_cancelled_api_result_is_not_published_but_usage_is_recorded(database, monkeypatch):
    vault = MemoryVault()
    vault.set("openai-api-key", "fictional-test-key")
    service = OpenAIService(vault, AppSettings(ai_backend="api"), UsageLedger(database, 10))
    recorded = []
    monkeypatch.setattr(service.usage, "record", lambda *args: recorded.append(args))
    def respond(**kwargs):
        assert kwargs["store"] is False
        service.cancel()
        return SimpleNamespace(output_text='{"value":"late"}',
                               usage=SimpleNamespace(input_tokens=12, output_tokens=4))
    monkeypatch.setattr(service, "_client", lambda: SimpleNamespace(responses=SimpleNamespace(create=respond)))
    with pytest.raises(OperationCancelled):
        service.structured(ExampleResult, operation="test", instructions="Status", input_text="fictional",
                           model="gpt-5.6-terra")
    assert recorded == [("test", "gpt-5.6-terra", 12, 4)]


def test_codex_is_automatic_fallback(database: Database, tmp_path: Path) -> None:
    executable = tmp_path / "codex"
    executable.touch(mode=0o700)
    service = OpenAIService(
        MemoryVault(), AppSettings(ai_backend="auto"), UsageLedger(database, 0), executable
    )

    assert service.backend_name() == "codex"


def test_explicit_codex_backend_never_reads_api_key(database: Database, tmp_path: Path) -> None:
    executable = tmp_path / "codex"
    executable.touch(mode=0o700)
    vault = MemoryVault()
    vault.get = lambda _name: (_ for _ in ()).throw(AssertionError("Keychain was read"))
    service = OpenAIService(
        vault, AppSettings(ai_backend="codex"), UsageLedger(database, 0), executable
    )
    assert service.backend_name() == "codex"


def test_codex_structured_output_is_validated_and_recorded(
    database: Database, tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "codex"
    executable.touch(mode=0o700)
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert "--output-last-message" not in command
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"value\\":\\"ready\\"}"}}\n'
                '{"type":"turn.completed","usage":{"input_tokens":120,"output_tokens":12}}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr(
        OpenAIService, "_run_codex", lambda self, command, prompt: fake_run(command, input=prompt)
    )
    service = OpenAIService(
        MemoryVault(), AppSettings(ai_backend="codex"), UsageLedger(database, 0), executable
    )

    result = service.structured(
        ExampleResult,
        operation="test",
        instructions="Return a status.",
        input_text="Untrusted email text",
        model="gpt-5.6-terra",
    )

    assert result.value == "ready"
    assert "--ephemeral" in commands[0]
    assert "features.shell_tool=false" in commands[0]
    assert 'web_search="disabled"' in commands[0]
    assert "agents.enabled=false" in commands[0]
    assert "apps._default.enabled=false" in commands[0]
    assert commands[0][commands[0].index("--sandbox") + 1] == "read-only"
    usage = database.query(
        "SELECT model,input_tokens,output_tokens,estimated_usd FROM usage_records"
    )
    assert dict(usage[0]) == {
        "model": "codex:gpt-5.6-terra",
        "input_tokens": 120,
        "output_tokens": 12,
        "estimated_usd": 0.0,
    }


def test_cancel_stops_only_the_owned_process(database, tmp_path):
    service = OpenAIService(
        MemoryVault(), AppSettings(), UsageLedger(database, 0), Path(sys.executable)
    )
    timer = threading.Timer(0.3, service.cancel)
    timer.start()
    started = time.monotonic()
    try:
        import pytest

        with pytest.raises(OperationCancelled):
            service._run_codex([sys.executable, "-c", "import time; time.sleep(30)"], "")
        assert time.monotonic() - started < 2
    finally:
        timer.join()


def test_compact_source_references_round_trip_without_changing_bodies():
    source = "11111111-2222-3333-4444-555555555555"
    text, aliases = AssistantService._short_references(
        f"[SOURCE:{source}] Original evidence", [source]
    )
    assert text == "[SOURCE:S1] Original evidence"
    result = DraftSet(
        decision_detected=False,
        candidates=[
            DraftCandidate(
                label=label,
                stance="Accept",
                body="Thanks; Tuesday works.",
                supporting_source_ids=["S1", "unknown"],
                tone_example_ids=["S1"],
            )
            for label in ["Concise", "Warm", "Detailed"]
        ],
    )
    result = AssistantService._restore_references(result, aliases)
    assert all(item.supporting_source_ids == [source] for item in result.candidates)
    assert all(item.body == "Thanks; Tuesday works." for item in result.candidates)
