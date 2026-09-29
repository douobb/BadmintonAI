"""TASK-006 Compose 設定的靜態展開與服務邊界測試。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _compose_config(tmp_path: Path) -> dict[str, object]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI 不可用")
    secret = tmp_path / "webui-secret"
    secret.write_text("test-only-secret", encoding="utf-8")
    evaluation_questions = tmp_path / "approved-evaluation-questions.txt"
    evaluation_questions.write_text("1: test question\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                f"BADMINTON_AI_SOURCE_DATA_HOST_DIR={tmp_path.as_posix()}",
                "BADMINTON_AI_DATA_FILE=events.csv",
                "BADMINTON_AI_METADATA_DIR=metadata",
                f"WEBUI_SECRET_KEY_FILE={secret.as_posix()}",
                "BADMINTON_AI_EVALUATION_QUESTIONS_HOST_FILE="
                f"{evaluation_questions.as_posix()}",
                "OPEN_WEBUI_HOST_PORT=3300",
                "BADMINTON_AI_TOOL_SERVER_HOST_PORT=8800",
                "BADMINTON_AI_OPEN_WEBUI_API_KEY=test-only-admin-key",
            )
        ),
        encoding="utf-8",
    )
    base_command = [
        docker,
        "compose",
        "--project-directory",
        str(PROJECT_ROOT),
        "--env-file",
        str(env_file),
    ]
    quiet = subprocess.run(
        [*base_command, "config", "--quiet"],
        capture_output=True,
        check=False,
    )
    assert quiet.returncode == 0, quiet.stderr.decode("utf-8", errors="replace")
    rendered = subprocess.run(
        [*base_command, "config", "--format", "json"],
        capture_output=True,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr.decode("utf-8", errors="replace")
    return json.loads(rendered.stdout.decode("utf-8"))


def test_compose_has_fixed_services_and_transport_boundaries(tmp_path: Path) -> None:
    config = _compose_config(tmp_path)
    evaluation_questions = tmp_path / "approved-evaluation-questions.txt"

    assert config["name"] == "badminton-ai-v2"
    services = config["services"]
    assert set(services) == {"tool-server", "open-webui"}

    tool = services["tool-server"]
    assert tool["image"] == "badminton-ai-tool-server:0.1.0"
    assert tool["build"]["dockerfile"] == "server/Dockerfile"
    assert tool["environment"]["BADMINTON_AI_PLOTLY_ASSET_URL"] == (
        "http://127.0.0.1:8800/assets/plotly-6.6.0.min.js"
    )
    assert tool["environment"]["BADMINTON_AI_OPEN_WEBUI_API_KEY"] == (
        "test-only-admin-key"
    )
    health_test = tool["healthcheck"]["test"]
    assert health_test[0] == "CMD"
    assert "data.get('status') == 'ok'" in health_test[-1]
    assert "degraded" not in health_test[-1]
    tool_targets = {item["target"]: item for item in tool["volumes"]}
    assert tool_targets["/var/lib/badminton-ai/source"]["read_only"] is True
    assert tool_targets["/var/run/docker.sock"]["source"] == "/var/run/docker.sock"
    assert "/var/lib/badminton-ai/runtime" not in tool_targets
    assert set(tool["networks"]) == {"badminton"}

    webui = services["open-webui"]
    assert webui["image"] == "badminton-ai-open-webui:0.11.3-safe-errors"
    assert webui["depends_on"]["tool-server"]["condition"] == "service_healthy"
    assert webui["environment"]["WEBUI_SECRET_KEY_FILE"] == (
        "/run/secrets/webui_secret_key"
    )
    assert webui["environment"]["CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS"] == "16"
    assert webui["environment"]["BADMINTON_AI_OPEN_WEBUI_API_KEY"] == (
        "test-only-admin-key"
    )
    assert "IFRAME_CSP" not in webui["environment"]
    assert webui["environment"]["BADMINTON_AI_EVALUATION_STORAGE_DIR"] == (
        "/var/lib/badminton-ai/evaluation"
    )
    assert webui["environment"]["BADMINTON_AI_EVALUATION_MODEL_ID"] == "badmintonai"
    webui_volumes = {item["target"]: item for item in webui["volumes"]}
    assert webui_volumes["/var/lib/badminton-ai/evaluation"]["type"] == "volume"
    assert webui_volumes["/var/lib/badminton-ai/evaluation"]["source"] == (
        "evaluation_data"
    )
    expected_mounts = {
        "/opt/badmintonai-evaluation/scripts/__init__.py": PROJECT_ROOT
        / "scripts"
        / "__init__.py",
        "/opt/badmintonai-evaluation/scripts/evaluation_runner.py": PROJECT_ROOT
        / "scripts"
        / "evaluation_runner.py",
        "/opt/badmintonai-evaluation/scripts/evaluation_openwebui_client.py": (
            PROJECT_ROOT / "scripts" / "evaluation_openwebui_client.py"
        ),
        "/opt/badmintonai-evaluation/scripts/evaluation_workbench_service.py": (
            PROJECT_ROOT / "scripts" / "evaluation_workbench_service.py"
        ),
        "/opt/badmintonai-evaluation/scripts/evaluation_report_html.py": (
            PROJECT_ROOT / "scripts" / "evaluation_report_html.py"
        ),
        "/opt/badmintonai-evaluation/scripts/export_chat_html.py": (
            PROJECT_ROOT / "scripts" / "export_chat_html.py"
        ),
        "/opt/badmintonai-evaluation/src/badminton_ai/server/plotly_rich.py": (
            PROJECT_ROOT / "src" / "badminton_ai" / "server" / "plotly_rich.py"
        ),
        "/opt/badmintonai-evaluation/src/badminton_ai/server/chart_display.py": (
            PROJECT_ROOT / "src" / "badminton_ai" / "server" / "chart_display.py"
        ),
        "/opt/badmintonai-evaluation/ui/evaluation_workbench.css": (
            PROJECT_ROOT / "openwebui_functions" / "evaluation_workbench.css"
        ),
        "/opt/badmintonai-evaluation/ui/evaluation_workbench.js": (
            PROJECT_ROOT / "openwebui_functions" / "evaluation_workbench.js"
        ),
        "/opt/badmintonai-evaluation/questions/評估問題_v2.txt": (evaluation_questions),
    }
    assert set(expected_mounts).issubset(webui_volumes)
    for target, expected_source in expected_mounts.items():
        mount = webui_volumes[target]
        assert mount["type"] == "bind"
        assert Path(mount["source"]).resolve() == expected_source.resolve()
        assert mount["read_only"] is True
    assert len([item for item in webui["volumes"] if item["type"] == "bind"]) == len(
        expected_mounts
    )
    assert not any(
        "BadmintonAI_v2" in str(item["source"])
        and Path(item["source"]).resolve() == PROJECT_ROOT.resolve()
        for item in webui["volumes"]
    )
    assert config["volumes"]["open_webui_data"]["name"] == (
        "badminton-ai-v2-open-webui-data"
    )
    assert config["volumes"]["evaluation_data"]["name"] == (
        "badminton-ai-v2-evaluation-data"
    )
    assert config["networks"]["badminton"]["name"] == "badminton-ai-v2-network"


def test_tool_server_dockerfile_locks_bases_and_copies_cli() -> None:
    dockerfile = (PROJECT_ROOT / "server" / "Dockerfile").read_text(encoding="utf-8")

    assert "docker:29.4.3-cli@sha256:" in dockerfile
    assert "python:3.12.14-slim-bookworm@sha256:" in dockerfile
    assert "COPY --from=docker-cli /usr/local/bin/docker" in dockerfile
    assert "pip install --no-cache-dir ." in dockerfile


def test_plotly_runtime_is_pinned_in_server_and_hash_locked_in_sandbox() -> None:
    project = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    requirements = (PROJECT_ROOT / "sandbox" / "requirements.txt").read_text(
        encoding="utf-8"
    )
    dockerfile = (PROJECT_ROOT / "sandbox" / "Dockerfile").read_text(encoding="utf-8")

    assert '"plotly==6.6.0"' in project
    assert "narwhals==2.18.1" in requirements
    assert "plotly==6.6.0" in requirements
    assert (
        "--hash=sha256:8d6daf0f87412e0c0bfe72e809d615217ab57cc715899a1e5145135a7800d1d0"
        in requirements
    )
    assert "--require-hashes" in dockerfile


def test_docker_context_excludes_local_secrets_and_private_docs() -> None:
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert ".env" in dockerignore
    assert ".env.*" in dockerignore
    assert "TODO.md" in dockerignore
    assert "docs/PRD.md" in dockerignore
    assert "docs/private/" in dockerignore
