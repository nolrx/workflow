import json

from backend.services.code.template_service import CodeTemplateService


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_select_frontend_template_from_local_repo(tmp_path):
    repo = tmp_path / "templates"
    react = repo / "frontend" / "react-vite-dashboard"
    vue = repo / "frontend" / "vue-admin"
    _write(
        react / "package.json",
        json.dumps(
            {
                "name": "react-vite-dashboard",
                "dependencies": {"react": "^19.0.0"},
                "devDependencies": {"vite": "^6.0.0", "typescript": "^5.0.0"},
            }
        ),
    )
    _write(react / "src" / "App.tsx", "export default function App() { return null }")
    _write(
        vue / "package.json",
        json.dumps({"name": "vue-admin", "dependencies": {"vue": "^3.0.0"}}),
    )
    _write(vue / "src" / "App.vue", "<template />")

    service = CodeTemplateService(repo_url=str(repo), cache_dir=str(tmp_path / "cache"))
    selection = service.select(
        lane="frontend",
        requirements_doc="## 技术架构建议\n- 前端: React + TypeScript + Vite。",
        documents_digest="## 前端实现文档\n需要 dashboard 页面。",
    )

    assert selection.selected
    assert selection.template_path == "frontend/react-vite-dashboard"
    assert "package.json" in selection.files
    assert "src/App.tsx" in selection.files


def test_select_backend_template_by_stack_keywords(tmp_path):
    repo = tmp_path / "templates"
    fastapi = repo / "backend" / "python-fastapi-postgres"
    express = repo / "backend" / "node-express"
    _write(fastapi / "pyproject.toml", "[project]\nname='api'\n")
    _write(fastapi / "Dockerfile", "FROM python:3.12-slim\n")
    _write(fastapi / "README.md", "FastAPI Postgres backend template")
    _write(
        express / "package.json",
        json.dumps({"name": "express-api", "dependencies": {"express": "^5.0.0"}}),
    )
    _write(express / "Dockerfile", "FROM node:22\n")

    service = CodeTemplateService(repo_url=str(repo), cache_dir=str(tmp_path / "cache"))
    selection = service.select(
        lane="backend",
        development_flow="## 技术假设\n后端采用 Python FastAPI, 数据库使用 Postgres。",
        documents_digest="## 后端实现文档\n需要 JWT 鉴权。",
    )

    assert selection.selected
    assert selection.template_path == "backend/python-fastapi-postgres"
    assert "pyproject.toml" in selection.files


def test_template_selection_fails_soft_for_missing_repo(tmp_path):
    service = CodeTemplateService(
        repo_url=str(tmp_path / "missing"),
        cache_dir=str(tmp_path / "cache"),
    )

    selection = service.select(lane="frontend", requirements_doc="React + Vite")

    assert not selection.selected
    assert selection.files == {}
    assert selection.warning
