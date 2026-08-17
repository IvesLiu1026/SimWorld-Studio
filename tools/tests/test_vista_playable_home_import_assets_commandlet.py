from __future__ import annotations

import ast
import json
import pathlib
import sys
import types

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[2]
COMMANDLET = ROOT / "tools/ue/vista_playable_home/import_assets_commandlet.py"
NAMESPACE = "/Game/VISTA/PlayableHome/r2"
SCHEMA = "simworld.vista.playable-home-native-nanite/v1"
MESH_A = NAMESPACE + "/Assets/A/A.A"
MESH_B = NAMESPACE + "/Assets/B/B.B"


class FakeMaterial:
    def __init__(self, blend_mode: str) -> None:
        self.blend_mode = blend_mode

    def get_base_material(self):
        return self

    def get_editor_property(self, name: str):
        if name == "blend_mode":
            return self.blend_mode
        raise AttributeError(name)


@pytest.fixture
def commandlet(monkeypatch: pytest.MonkeyPatch):
    unreal = types.ModuleType("unreal")
    unreal.StaticMesh = type("FakeStaticMesh", (), {})
    unreal.Texture2D = type("FakeTexture2D", (), {})
    unreal.BlendMode = types.SimpleNamespace(
        BLEND_OPAQUE="BLEND_OPAQUE",
        BLEND_MASKED="BLEND_MASKED",
        BLEND_TRANSLUCENT="BLEND_TRANSLUCENT",
    )

    class VistaPlayableHomeNaniteLibrary:
        calls = []
        response = ""

        @classmethod
        def finalize_nanite_policies(cls, namespace, mesh_paths):
            cls.calls.append((namespace, list(mesh_paths)))
            return cls.response

    unreal.VistaPlayableHomeNaniteLibrary = VistaPlayableHomeNaniteLibrary
    monkeypatch.setitem(sys.modules, "unreal", unreal)
    tree = ast.parse(COMMANDLET.read_text(encoding="utf-8"), filename=str(COMMANDLET))
    final = tree.body[-1]
    assert (
        isinstance(final, ast.Expr)
        and isinstance(final.value, ast.Call)
        and isinstance(final.value.func, ast.Name)
        and final.value.func.id == "run"
    )
    tree.body.pop()
    module = types.ModuleType("vista_import_assets_commandlet_test")
    module.__file__ = str(COMMANDLET)
    exec(compile(tree, str(COMMANDLET), "exec"), module.__dict__)
    return module, unreal


def imported_item(
    object_path: str,
    *,
    source_kind: str = "generated",
    policy: str = "eligible_static_opaque",
    enabled: bool | None = False,
) -> dict:
    return {
        "source_kind": source_kind,
        "object_path": object_path,
        "inspection": {
            "object_path": object_path,
            "material_blend_modes": ["BLEND_OPAQUE"],
            "nanite_policy": policy,
            "nanite_enabled": enabled,
        },
    }


def native_result(
    object_path: str,
    *,
    modes: list[str] | None = None,
    policy: str = "eligible_static_opaque",
    enabled: bool = True,
) -> dict:
    return {
        "object_path": object_path,
        "material_blend_modes": modes or ["BLEND_OPAQUE"],
        "nanite_policy": policy,
        "nanite_enabled": enabled,
    }


def response(results: list[dict]) -> str:
    return json.dumps(
        {"schema_version": SCHEMA, "status": "success", "results": results},
        separators=(",", ":"),
    )


def test_initial_nanite_inspection_classifies_without_mutation(commandlet) -> None:
    module, _ = commandlet
    opaque = FakeMaterial("BLEND_OPAQUE")
    translucent = FakeMaterial("BLEND_TRANSLUCENT")

    modes, nonopaque = module.classify_nanite_material_policy(
        [opaque, translucent]
    )

    assert modes == ["BLEND_OPAQUE", "BLEND_TRANSLUCENT"]
    assert nonopaque is True
    assert opaque.blend_mode == "BLEND_OPAQUE"
    assert translucent.blend_mode == "BLEND_TRANSLUCENT"


def test_native_bridge_called_once_and_results_joined_by_object_path(
    commandlet,
) -> None:
    module, unreal = commandlet
    builtin = imported_item(
        NAMESPACE + "/Assets/Pawn/Pawn.Pawn",
        source_kind="builtin",
        policy="not_applicable",
        enabled=None,
    )
    item_b = imported_item(MESH_B)
    item_a = imported_item(MESH_A)
    unreal.VistaPlayableHomeNaniteLibrary.response = response([
        native_result(MESH_A),
        native_result(
            MESH_B,
            modes=["BLEND_TRANSLUCENT"],
            policy="disabled_nonopaque_material",
            enabled=False,
        ),
    ])

    module.finalize_nanite_policies(NAMESPACE, [builtin, item_b, item_a])

    assert unreal.VistaPlayableHomeNaniteLibrary.calls == [
        (NAMESPACE, [MESH_A, MESH_B])
    ]
    assert item_a["inspection"] == {
        "object_path": MESH_A,
        "material_blend_modes": ["BLEND_OPAQUE"],
        "nanite_policy": "eligible_static_opaque",
        "nanite_enabled": True,
    }
    assert item_b["inspection"] == {
        "object_path": MESH_B,
        "material_blend_modes": ["BLEND_TRANSLUCENT"],
        "nanite_policy": "disabled_nonopaque_material",
        "nanite_enabled": False,
    }
    assert builtin["inspection"]["nanite_policy"] == "not_applicable"
    assert builtin["inspection"]["nanite_enabled"] is None


@pytest.mark.parametrize(
    "native_response",
    [
        "not-json",
        "[]",
        json.dumps({"schema_version": "wrong", "status": "success", "results": []}),
        json.dumps({"schema_version": SCHEMA, "status": "error", "error": "failed"}),
        json.dumps({"schema_version": SCHEMA, "status": "success", "results": {}}),
        json.dumps({
            "schema_version": SCHEMA,
            "status": "success",
            "results": [{
                "object_path": MESH_A,
                "material_blend_modes": ["opaque"],
                "nanite_policy": "eligible_static_opaque",
                "nanite_enabled": True,
            }],
        }),
        (
            '{"schema_version":"' + SCHEMA + '","schema_version":"' + SCHEMA
            + '","status":"success","results":[]}'
        ),
    ],
)
def test_native_bridge_malformed_payload_fails_closed(
    commandlet, native_response: str
) -> None:
    module, unreal = commandlet
    item = imported_item(MESH_A)
    original = dict(item["inspection"])
    unreal.VistaPlayableHomeNaniteLibrary.response = native_response

    with pytest.raises(RuntimeError):
        module.finalize_nanite_policies(NAMESPACE, [item])

    assert unreal.VistaPlayableHomeNaniteLibrary.calls == [
        (NAMESPACE, [MESH_A])
    ]
    assert item["inspection"] == original


def test_native_bridge_missing_result_path_fails_before_receipt_update(
    commandlet,
) -> None:
    module, unreal = commandlet
    item_a = imported_item(MESH_A)
    item_b = imported_item(MESH_B)
    originals = [dict(item_a["inspection"]), dict(item_b["inspection"])]
    unreal.VistaPlayableHomeNaniteLibrary.response = response([
        native_result(MESH_A)
    ])

    with pytest.raises(RuntimeError, match="incomplete"):
        module.finalize_nanite_policies(NAMESPACE, [item_a, item_b])

    assert item_a["inspection"] == originals[0]
    assert item_b["inspection"] == originals[1]


def test_native_bridge_duplicate_result_path_fails_before_receipt_update(
    commandlet,
) -> None:
    module, unreal = commandlet
    item_a = imported_item(MESH_A)
    item_b = imported_item(MESH_B)
    originals = [dict(item_a["inspection"]), dict(item_b["inspection"])]
    unreal.VistaPlayableHomeNaniteLibrary.response = response([
        native_result(MESH_A),
        native_result(MESH_A),
    ])

    with pytest.raises(RuntimeError, match="duplicated"):
        module.finalize_nanite_policies(NAMESPACE, [item_a, item_b])

    assert item_a["inspection"] == originals[0]
    assert item_b["inspection"] == originals[1]


def test_native_bridge_unsorted_results_fail_before_receipt_update(
    commandlet,
) -> None:
    module, unreal = commandlet
    item_a = imported_item(MESH_A)
    item_b = imported_item(MESH_B)
    originals = [dict(item_a["inspection"]), dict(item_b["inspection"])]
    unreal.VistaPlayableHomeNaniteLibrary.response = response([
        native_result(MESH_B),
        native_result(MESH_A),
    ])

    with pytest.raises(RuntimeError, match="deterministically sorted"):
        module.finalize_nanite_policies(NAMESPACE, [item_a, item_b])

    assert item_a["inspection"] == originals[0]
    assert item_b["inspection"] == originals[1]


@pytest.mark.parametrize("object_path", [None, 7, MESH_B])
def test_native_bridge_invalid_or_unexpected_result_path_fails_closed(
    commandlet, object_path
) -> None:
    module, unreal = commandlet
    item = imported_item(MESH_A)
    original = dict(item["inspection"])
    unreal.VistaPlayableHomeNaniteLibrary.response = response([
        native_result(object_path)
    ])

    with pytest.raises(RuntimeError, match="object path"):
        module.finalize_nanite_policies(NAMESPACE, [item])

    assert item["inspection"] == original
