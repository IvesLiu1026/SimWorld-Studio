from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest


from tools.blender.vista_playable_home_realism import acquire_poly_haven as acquisition


def _manifest() -> dict:
    return {
        "schema_version": acquisition.SCHEMA_VERSION,
        "assets": [
            {
                "asset_id": "sofa_02",
                "logical_asset_id": "visual.hero.living_sofa",
                "asset_type": "model",
                "expected_files_hash": "a" * 40,
                "resolution": "2k",
                "file_variant": "blend",
                "room_role": "living_hero",
            }
        ],
    }


def _catalog() -> dict:
    return {
        "sofa_02": {
            "type": 2,
            "name": "Sofa 02",
            "description": "Free CC0 sofa",
            "date_published": 123,
            "max_resolution": [4096, 4096],
            "polycount": 2728,
            "texel_density": 2200.0,
            "dimensions": [1807.0, 817.0, 709.0],
            "files_hash": "a" * 40,
        }
    }


def _files(*, host: str = acquisition.ALLOWED_DOWNLOAD_HOST, include_path: str = "textures/sofa_diff.jpg") -> dict:
    return {
        "blend": {
            "2k": {
                "blend": {
                    "include": {
                        include_path: {
                            "size": 9,
                            "url": f"https://{host}/file/ph-assets/sofa_diff.jpg",
                            "md5": "b" * 32,
                        }
                    },
                    "size": 12,
                    "url": f"https://{host}/file/ph-assets/sofa_02_2k.blend",
                    "md5": "c" * 32,
                }
            }
        }
    }


def _texture_files() -> dict:
    result = {}
    for semantic, suffix in (("Diffuse", "diff"), ("nor_gl", "nor_gl"), ("Rough", "rough")):
        result[semantic] = {
            "4k": {
                "jpg": {
                    "size": 17,
                    "url": f"https://{acquisition.ALLOWED_DOWNLOAD_HOST}/file/ph-assets/oak_{suffix}_4k.jpg",
                    "md5": "d" * 32,
                }
            }
        }
    return result


def test_closed_request_and_download_plan(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    raw, requests = acquisition.load_request(path)
    assert hashlib.sha256(raw).hexdigest()
    assert requests[0].logical_asset_id == "visual.hero.living_sofa"
    plan = acquisition.build_download_plan(requests[0], _catalog(), _files())
    assert plan.files_hash == "a" * 40
    assert [item.relative_path for item in plan.files] == [
        "sofa_02_2k.blend",
        "textures/sofa_diff.jpg",
    ]
    assert sum(item.expected_size for item in plan.files) == 21


def test_texture_pbr_triplet_plan(tmp_path: Path) -> None:
    payload = _manifest()
    payload["assets"][0].update(
        {
            "asset_id": "white_oak_veneer",
            "logical_asset_id": "visual.material.white_oak_veneer",
            "asset_type": "texture",
            "expected_files_hash": "e" * 40,
            "resolution": "4k",
            "file_variant": "pbr_jpg",
            "room_role": "authored_hero_material",
        }
    )
    path = tmp_path / "texture.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    _, requests = acquisition.load_request(path)
    catalog = _catalog()
    catalog["white_oak_veneer"] = {
        **catalog.pop("sofa_02"),
        "type": 1,
        "files_hash": "e" * 40,
    }
    plan = acquisition.build_download_plan(requests[0], catalog, _texture_files())
    assert [item.relative_path for item in plan.files] == [
        "oak_diff_4k.jpg",
        "oak_nor_gl_4k.jpg",
        "oak_rough_4k.jpg",
    ]


def test_request_rejects_unknown_fields_and_duplicate_identity(tmp_path: Path) -> None:
    payload = _manifest()
    payload["unexpected"] = True
    path = tmp_path / "unknown.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(acquisition.AcquisitionError, match="closed contract"):
        acquisition.load_request(path)

    duplicate = _manifest()
    duplicate["assets"].append(dict(duplicate["assets"][0]))
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps(duplicate), encoding="utf-8")
    with pytest.raises(acquisition.AcquisitionError, match="unique"):
        acquisition.load_request(path)


@pytest.mark.parametrize(
    ("files", "message"),
    [
        (_files(host="evil.example"), "official HTTPS host"),
        (_files(include_path="../escape.jpg"), "safe relative path"),
        ({"blend": {"1k": _files()["blend"]["2k"]}}, "unavailable"),
    ],
)
def test_download_plan_fails_closed(files: dict, message: str, tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    _, requests = acquisition.load_request(path)
    with pytest.raises(acquisition.AcquisitionError, match=message):
        acquisition.build_download_plan(requests[0], _catalog(), files)


def test_download_plan_rejects_upstream_files_hash_drift(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    _, requests = acquisition.load_request(path)
    catalog = _catalog()
    catalog["sofa_02"]["files_hash"] = "9" * 40
    with pytest.raises(acquisition.AcquisitionError, match="changed from the pinned manifest"):
        acquisition.build_download_plan(requests[0], catalog, _files())


@pytest.mark.parametrize(
    "url",
    [
        "https://dl.polyhaven.org:444/file/ph-assets/model.blend",
        "https://dl.polyhaven.org/file/ph-assets/model.blend?token=secret",
        "https://dl.polyhaven.org/not-the-official-prefix/model.blend",
    ],
)
def test_download_urls_reject_ports_queries_and_wrong_paths(url: str) -> None:
    with pytest.raises(acquisition.AcquisitionError):
        acquisition._safe_https_url(
            url,
            host=acquisition.ALLOWED_DOWNLOAD_HOST,
            label="fixture",
        )


def test_download_file_verifies_size_md5_sha_and_cleans_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"exact-provider-bytes"
    item = acquisition.DownloadFile(
        relative_path="model.blend",
        url="https://dl.polyhaven.org/file/ph-assets/model.blend",
        expected_size=len(payload),
        expected_md5=hashlib.md5(payload).hexdigest(),
    )
    monkeypatch.setattr(acquisition, "_request", lambda *_args, **_kwargs: io.BytesIO(payload))
    destination = tmp_path / "asset" / "model.blend"
    receipt = acquisition._download_file(item, destination)
    assert destination.read_bytes() == payload
    assert receipt["size_bytes"] == len(payload)
    assert receipt["sha256"] == hashlib.sha256(payload).hexdigest()
    assert not destination.with_name(".model.blend.partial").exists()

    bad = acquisition.DownloadFile(
        relative_path=item.relative_path,
        url=item.url,
        expected_size=len(payload),
        expected_md5="0" * 32,
    )
    rejected = tmp_path / "rejected" / "model.blend"
    with pytest.raises(acquisition.AcquisitionError, match="integrity mismatch"):
        acquisition._download_file(bad, rejected)
    assert not rejected.exists()
    assert not rejected.with_name(".model.blend.partial").exists()

    wrong_size = acquisition.DownloadFile(
        relative_path=item.relative_path,
        url=item.url,
        expected_size=len(payload) + 1,
        expected_md5=item.expected_md5,
    )
    rejected_size = tmp_path / "rejected-size" / "model.blend"
    with pytest.raises(acquisition.AcquisitionError, match="integrity mismatch"):
        acquisition._download_file(wrong_size, rejected_size)
    assert not rejected_size.exists()
    assert not rejected_size.with_name(".model.blend.partial").exists()


def test_acquire_writes_hash_bound_aggregate_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"aggregate-source"
    request = acquisition.RequestedAsset(
        asset_id="fixture_model",
        logical_asset_id="visual.dressing.fixture_model",
        asset_type="model",
        expected_files_hash="f" * 40,
        resolution="2k",
        file_variant="blend",
        room_role="fixture",
    )
    file = acquisition.DownloadFile(
        relative_path="fixture.blend",
        url="https://dl.polyhaven.org/file/ph-assets/fixture.blend",
        expected_size=len(payload),
        expected_md5=hashlib.md5(payload).hexdigest(),
    )
    plan = acquisition.DownloadPlan(
        request=request,
        catalog_record={"name": "Fixture"},
        files_hash=request.expected_files_hash,
        files=(file,),
    )
    manifest_raw = b'{"fixture":true}'
    monkeypatch.setattr(acquisition, "_resolve_plans", lambda _path: (manifest_raw, (plan,)))
    monkeypatch.setattr(acquisition, "_request", lambda *_args, **_kwargs: io.BytesIO(payload))
    root = tmp_path / "attempt-01"
    receipt = acquisition.acquire(tmp_path / "manifest.json", root)
    serialized = json.loads((root / "acquisition-receipt.json").read_text(encoding="utf-8"))
    assert serialized == receipt
    assert receipt["asset_count"] == 1
    assert receipt["total_size_bytes"] == len(payload)
    assert receipt["assets"][0]["asset_type"] == "model"
    assert receipt["assets"][0]["files"][0]["sha256"] == hashlib.sha256(payload).hexdigest()
    body = dict(receipt)
    digest = body.pop("receipt_digest")
    assert digest == acquisition._sha256_bytes(acquisition._canonical_json(body))


def test_fresh_output_root_rejects_reuse_and_relative_paths(tmp_path: Path) -> None:
    with pytest.raises(acquisition.AcquisitionError, match="absolute"):
        acquisition._prepare_output_root(Path("relative"))
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(acquisition.AcquisitionError, match="fresh"):
        acquisition._prepare_output_root(existing)
