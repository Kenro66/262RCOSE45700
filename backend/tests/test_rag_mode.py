"""实际配置决定入口，默认与原 Single HTTP 契约保持兼容。"""
import pytest
import os
from pathlib import Path
import subprocess
import sys
from httpx import ASGITransport, AsyncClient

import app.main as main


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    from app.adapters.config import settings
    monkeypatch.delenv("RAG_MODE", raising=False)
    monkeypatch.setattr(main, "settings", lambda: {key: value for key, value in settings().items() if key != "RAG_MODE"}, raising=False)


@pytest.mark.parametrize("mode", [None, "single", "agent"])
@pytest.mark.asyncio
async def test_mode_selects_real_entry_and_preserves_empty_document_contract(db, monkeypatch, mode):
    monkeypatch.setattr(main, "settings", lambda: {} if mode is None else {"RAG_MODE": mode})
    application = main.create_app()
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        result = await client.post("/api/chat", json={"message": "政策是什么？", "session_id": "mode-test"})
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["status"] == "NO_READY_DOCUMENTS"
        if mode == "agent":
            assert body["session_id"] == "mode-test" and body["question_id"]
            assert body["execution_status"] == "SUCCEEDED"
            assert (await client.get("/api/chat/sessions")).status_code == 200
        else:
            assert body == {"status": "NO_READY_DOCUMENTS", "answer": None, "citations": [], "model_name": None, "message": "当前没有可用文件"}
            assert (await client.get("/api/chat/sessions")).status_code == 404
    if mode == "agent":
        await application.state.chat_runner.close()


@pytest.mark.parametrize("value", ["", " ", "AGENT", "fallback", "invalid"])
def test_invalid_mode_fails_during_app_creation(monkeypatch, value):
    monkeypatch.setattr(main, "settings", lambda: {"RAG_MODE": value})
    with pytest.raises(ValueError, match="RAG_MODE.*single.*agent"):
        main.create_app()


@pytest.mark.parametrize("mode", ["single", "agent"])
def test_custom_answer_keeps_old_router_in_both_modes(monkeypatch, mode):
    monkeypatch.setattr(main, "settings", lambda: {"RAG_MODE": mode})
    application = main.create_app(answer_fn=lambda *_: None)
    assert not hasattr(application.state, "chat_runner")
    assert "/api/chat/sessions" not in application.openapi()["paths"]


def test_process_environment_selects_mode_over_dotenv(monkeypatch):
    import app.adapters.config as config
    monkeypatch.setattr(config, "dotenv_values", lambda _: {"RAG_MODE": "single"})
    monkeypatch.setenv("RAG_MODE", "agent")
    monkeypatch.setattr(main, "settings", config.settings)
    assert hasattr(main.create_app().state, "chat_runner")


@pytest.mark.parametrize("mode", ["single", "agent", "invalid"])
def test_real_module_startup_obeys_process_configuration(mode):
    environment = {**os.environ, "RAG_MODE": mode}
    command = "import app.main; assert hasattr(app.main.app.state, 'chat_runner') == " + repr(mode == "agent")
    result = subprocess.run([sys.executable, "-c", command], cwd=Path(__file__).resolve().parents[1],
                            env=environment, capture_output=True, text=True, timeout=25)
    if mode == "invalid":
        assert result.returncode != 0 and "RAG_MODE" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
