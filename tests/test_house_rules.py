"""Unit tests for the deterministic house-rules linter (no network/Docker)."""
from backend.services.code import house_rules as hr


# --- frontend ---------------------------------------------------------------
def test_fe_browser_router_is_error():
    files = {"src/main.tsx": b"import { BrowserRouter } from 'react-router-dom'\n"}
    vs = hr.check_frontend(files)
    assert any(v.rule_id == "fe-no-browser-router" and v.severity == hr.SEVERITY_ERROR for v in vs)
    assert hr.has_blocking(vs)


def test_fe_create_browser_router_is_error():
    files = {"src/router.ts": "export const r = createBrowserRouter([])\n"}
    assert any(v.rule_id == "fe-no-browser-router" for v in hr.check_frontend(files))


def test_fe_hash_router_is_clean():
    files = {"src/main.tsx": b"import { HashRouter } from 'react-router-dom'\n"}
    vs = hr.check_frontend(files)
    assert not hr.errors(vs)
    assert not hr.has_blocking(vs)


def test_fe_tailwind_dependency_is_error():
    files = {"package.json": '{"devDependencies": {"tailwindcss": "^3.4.0"}}'}
    assert any(v.rule_id == "fe-no-tailwind" for v in hr.check_frontend(files))


def test_fe_tailwind_directive_is_error():
    files = {"src/index.css": "@tailwind base;\n@tailwind components;\n"}
    assert any(v.rule_id == "fe-no-tailwind" for v in hr.check_frontend(files))


def test_fe_tailwind_config_file_is_error():
    files = {"tailwind.config.js": "module.exports = {}\n"}
    assert any(v.rule_id == "fe-no-tailwind" for v in hr.check_frontend(files))


def test_fe_remote_google_font_is_error():
    files = {"index.html": '<link href="https://fonts.googleapis.com/css2?family=Inter">'}
    assert any(v.rule_id == "fe-no-remote-fonts" for v in hr.check_frontend(files))


def test_fe_system_font_css_is_clean():
    files = {"src/index.css": "body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif}"}
    assert not hr.errors(hr.check_frontend(files))


def test_fe_lockfile_cnpm_mirror_is_error():
    files = {
        "package-lock.json": '{"resolved": "https://registry.npmmirror.com/react/-/react-19.0.0.tgz"}'
    }
    assert any(v.rule_id == "fe-lockfile-mirror" for v in hr.check_frontend(files))


def test_fe_official_lockfile_is_clean():
    files = {
        "package-lock.json": '{"resolved": "https://registry.npmjs.org/react/-/react-19.0.0.tgz"}'
    }
    assert not hr.errors(hr.check_frontend(files))


def test_fe_binary_asset_is_skipped():
    # A PNG byte blob containing the literal "BrowserRouter" must not trip the rule.
    files = {"src/assets/logo.png": b"\x89PNG\r\n\x1a\n BrowserRouter \x00\x01"}
    assert not hr.check_frontend(files)


# --- backend ----------------------------------------------------------------
def test_be_flask_api_prefix_is_error():
    files = {"app.py": '@app.route("/api/users")\ndef users():\n    return []\n'}
    vs = hr.check_backend(files)
    assert any(v.rule_id == "be-no-api-prefix" and v.severity == hr.SEVERITY_ERROR for v in vs)


def test_be_fastapi_router_prefix_is_error():
    files = {"routes.py": 'router = APIRouter(prefix="/api/v1")\n'}
    assert any(v.rule_id == "be-no-api-prefix" for v in hr.check_backend(files))


def test_be_express_api_prefix_is_error():
    files = {"server.js": 'app.use("/api", router)\n'}
    assert any(v.rule_id == "be-no-api-prefix" for v in hr.check_backend(files))


def test_be_root_mounted_route_is_clean():
    files = {"app.py": '@app.route("/auth/login", methods=["POST"])\ndef login():\n    return {}\n'}
    assert not hr.errors(hr.check_backend(files))


def test_be_async_driver_mismatch_is_warning():
    files = {"db.py": 'engine = create_async_engine("postgresql://u:p@h/db")\n'}
    vs = hr.check_backend(files)
    assert any(v.rule_id == "be-async-driver-mismatch" and v.severity == hr.SEVERITY_WARNING for v in vs)
    assert not hr.has_blocking(vs)  # warning only


def test_be_asyncpg_url_is_clean():
    files = {"db.py": 'engine = create_async_engine("postgresql+asyncpg://u:p@h/db")\n'}
    assert not hr.warnings(hr.check_backend(files))


def test_be_hardcoded_sslmode_is_warning():
    files = {"config.py": 'DSN = "postgresql://u:p@h/db?sslmode=require"\n'}
    assert any(v.rule_id == "be-hardcoded-sslmode" for v in hr.check_backend(files))


# --- reporting --------------------------------------------------------------
def test_render_report_groups_and_includes_remediation():
    files = {
        "src/main.tsx": "import { BrowserRouter } from 'react-router-dom'",
        "db.py": 'engine = create_async_engine("postgresql://x")',
    }
    vs = hr.check_frontend(files) + hr.check_backend(files)
    report = hr.render_report(vs)
    assert "House Rules" in report
    assert "必须修复" in report  # error group present
    assert "HashRouter" in report  # remediation text present


def test_render_report_empty_when_no_violations():
    assert hr.render_report([]) == ""


def test_summarize_counts():
    files = {"src/main.tsx": "createBrowserRouter([])", "config.py": "sslmode=require"}
    vs = hr.check_frontend(files) + hr.check_backend(files)
    s = hr.summarize(vs)
    assert s["errors"] >= 1 and s["warnings"] >= 1
    assert "fe-no-browser-router" in s["rule_ids"]
