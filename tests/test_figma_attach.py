"""
Route-level tests for attaching a Figma design to a Code project.

Mocks the Figma REST client + credential decryption (no network / no real PAT)
and exercises the exact endpoints the frontend hits: attach (all frames) → get →
delete. Verifies frame enumeration, render persistence, and the CodeFigmaDesign
UPSERT.
"""
import pytest

from backend.app import create_app
from backend.extensions import db


class _FakeFigma:
    """Stand-in for FigmaService: two frames across one canvas."""

    def __init__(self, token):
        pass

    def get_file(self, file_key, depth=None):
        return {
            "name": "My File",
            "document": {
                "children": [
                    {
                        "type": "CANVAS",
                        "children": [
                            {
                                "type": "FRAME",
                                "id": "1:2",
                                "name": "Home",
                                "absoluteBoundingBox": {"width": 375, "height": 812},
                            },
                            {"type": "TEXT", "id": "1:9", "name": "ignore me"},
                            {
                                "type": "FRAME",
                                "id": "1:3",
                                "name": "Detail",
                                "absoluteBoundingBox": {"width": 375, "height": 812},
                            },
                        ],
                    }
                ]
            },
        }

    def get_image_urls(self, file_key, node_ids, scale=2.0, fmt="png"):
        return {nid: f"https://img.example/{nid}" for nid in node_ids}

    @staticmethod
    def download_image(url):
        return b"\x89PNG\r\n\x1a\nFAKEPNG"


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    application = create_app("testing")
    application.config["UPLOAD_FOLDER"] = str(tmp_path / "uploads")
    with application.app_context():
        db.create_all()

        from flask_jwt_extended import create_access_token

        from backend.models.code import CodeProject, CodeProjectStatus, FigmaCredential

        user_id = "u-figma"
        project = CodeProject(
            user_id=user_id,
            title="T",
            requirement_input="r",
            requirements_doc="# 需求",
            style_prompt="s",
            status=CodeProjectStatus.UI_CONFIRMED,
        )
        db.session.add(project)
        db.session.add(
            FigmaCredential(user_id=user_id, token_encrypted="dummy", token_last4="test")
        )
        db.session.commit()

        # Mock the Figma client + credential decryption (no network / real PAT).
        import backend.services.code.figma.crypto as crypto
        import backend.services.code.figma_attach_service as attach_svc

        monkeypatch.setattr(attach_svc, "FigmaService", _FakeFigma)
        monkeypatch.setattr(crypto, "decrypt_token", lambda _enc: "figd_test")

        yield {
            "client": application.test_client(),
            "headers": {"Authorization": f"Bearer {create_access_token(identity=user_id)}"},
            "pid": project.id,
            "uploads": tmp_path / "uploads",
        }
        db.session.remove()
        db.drop_all()


def test_attach_get_detach_roundtrip(ctx):
    client, headers, pid = ctx["client"], ctx["headers"], ctx["pid"]

    # Attach: pulls all frames, renders, stores.
    resp = client.post(
        f"/api/code/figma/projects/{pid}/attach",
        json={"figma_url": "https://www.figma.com/design/ABC123/My-File"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    design = resp.get_json()["data"]["design"]
    assert design["count"] == 2  # the TEXT node was skipped
    names = [f["name"] for f in design["frames"]]
    assert names == ["Home", "Detail"]

    # Render PNGs were written to disk.
    render_dir = ctx["uploads"] / "figma_designs" / pid
    pngs = list(render_dir.glob("*.png"))
    assert len(pngs) == 2

    # The stored IR text is internal (never leaked through the API view).
    assert "ir_text" not in design["frames"][0]

    # Get
    resp = client.get(f"/api/code/figma/projects/{pid}/design", headers=headers)
    assert resp.status_code == 200
    assert resp.get_json()["data"]["design"]["count"] == 2

    # Re-attach replaces (still one design row, count stays 2).
    resp = client.post(
        f"/api/code/figma/projects/{pid}/attach",
        json={"figma_url": "https://www.figma.com/design/ABC123/My-File"},
        headers=headers,
    )
    assert resp.status_code == 200
    from backend.models.code import CodeFigmaDesign

    assert CodeFigmaDesign.query.filter_by(project_id=pid).count() == 1

    # Delete
    resp = client.delete(f"/api/code/figma/projects/{pid}/design", headers=headers)
    assert resp.status_code == 200
    resp = client.get(f"/api/code/figma/projects/{pid}/design", headers=headers)
    assert resp.get_json()["data"]["design"] is None


def test_attach_requires_figma_url(ctx):
    client, headers, pid = ctx["client"], ctx["headers"], ctx["pid"]
    resp = client.post(
        f"/api/code/figma/projects/{pid}/attach", json={}, headers=headers
    )
    assert resp.status_code == 400
