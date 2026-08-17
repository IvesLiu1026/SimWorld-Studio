from __future__ import annotations

import ast
import pathlib
import sys
import types

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[2]
COMMANDLET = ROOT / "tools/ue/vista_playable_home/import_assets_commandlet.py"


class FakeNaniteSettings:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def get_editor_property(self, name: str):
        if name == "enabled":
            return self.enabled
        raise AttributeError(name)

    def set_editor_property(self, name: str, value) -> None:
        if name != "enabled":
            raise AttributeError(name)
        self.enabled = value


class FakeMaterial:
    def __init__(
        self,
        *,
        used_with_nanite: bool,
        blend_mode: str = "BLEND_OPAQUE",
        path: str = (
            "/Game/VISTA/PlayableHome/r2/Assets/Fixture/"
            "InterchangeAssets/M_Fixture.M_Fixture"
        ),
        refuse_usage_edit: bool = False,
    ) -> None:
        self.used_with_nanite = used_with_nanite
        self.blend_mode = blend_mode
        self.path = path
        self.refuse_usage_edit = refuse_usage_edit
        self.modified = False
        self.post_edit_changed = False

    def get_base_material(self):
        return self

    def get_path_name(self) -> str:
        return self.path

    def get_editor_property(self, name: str):
        if name == "used_with_nanite":
            return self.used_with_nanite
        if name == "blend_mode":
            return self.blend_mode
        raise AttributeError(name)

    def set_editor_property(self, name: str, value) -> None:
        if name != "used_with_nanite" or self.refuse_usage_edit:
            raise RuntimeError("usage edit refused")
        self.used_with_nanite = value

    def modify(self) -> None:
        self.modified = True

    def post_edit_change(self) -> None:
        self.post_edit_changed = True


class FakeMaterialInstance:
    def __init__(self, base: FakeMaterial) -> None:
        self.base = base

    def get_base_material(self) -> FakeMaterial:
        return self.base

    def get_editor_property(self, name: str):
        if name == "blend_mode":
            return self.base.blend_mode
        raise AttributeError(name)


class FakeStaticMesh:
    def __init__(self, enabled: bool = True) -> None:
        self.settings = FakeNaniteSettings(enabled)

    def get_editor_property(self, name: str):
        if name == "nanite_settings":
            return self.settings
        raise AttributeError(name)

    def set_editor_property(self, name: str, value) -> None:
        if name != "nanite_settings":
            raise AttributeError(name)
        self.settings = value


@pytest.fixture
def commandlet(monkeypatch: pytest.MonkeyPatch):
    unreal = types.ModuleType("unreal")
    unreal.Material = FakeMaterial
    unreal.MaterialInstanceConstant = FakeMaterialInstance
    unreal.StaticMesh = FakeStaticMesh
    unreal.Texture2D = type("FakeTexture2D", (), {})
    unreal.BlendMode = types.SimpleNamespace(
        BLEND_OPAQUE="BLEND_OPAQUE",
        BLEND_MASKED="BLEND_MASKED",
        BLEND_TRANSLUCENT="BLEND_TRANSLUCENT",
    )
    unreal.MaterialUsage = types.SimpleNamespace(MATUSAGE_NANITE="MATUSAGE_NANITE")

    class MaterialEditingLibrary:
        usage_calls = []

        @classmethod
        def set_material_usage(cls, material, usage):
            assert usage == unreal.MaterialUsage.MATUSAGE_NANITE
            cls.usage_calls.append((material, usage))
            material.set_editor_property("used_with_nanite", True)

        @classmethod
        def has_material_usage(cls, material, usage):
            assert usage == unreal.MaterialUsage.MATUSAGE_NANITE
            return material.used_with_nanite

    unreal.MaterialEditingLibrary = MaterialEditingLibrary

    class EditorAssetLibrary:
        saved = []

        @classmethod
        def save_loaded_asset(cls, asset, *, only_if_is_dirty: bool):
            assert only_if_is_dirty is False
            cls.saved.append(asset)
            return True

    unreal.EditorAssetLibrary = EditorAssetLibrary
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


def test_nanite_usage_is_persisted_on_effective_base_material(commandlet) -> None:
    module, unreal = commandlet
    base = FakeMaterial(used_with_nanite=False)
    interface = FakeMaterialInstance(base)
    mesh = FakeStaticMesh(enabled=True)

    result = module.enforce_nanite_material_policy(mesh, [interface])

    assert result == {
        "material_blend_modes": ["BLEND_OPAQUE"],
        "nanite_policy": "eligible_static_opaque",
        "nanite_enabled": True,
    }
    assert base.used_with_nanite is True
    assert base.modified is True
    assert base.post_edit_changed is True
    assert unreal.MaterialEditingLibrary.usage_calls == [
        (base, unreal.MaterialUsage.MATUSAGE_NANITE)
    ]
    assert unreal.EditorAssetLibrary.saved == [base, mesh]


def test_initially_disabled_opaque_mesh_is_enabled_after_usage_proof(
    commandlet,
) -> None:
    module, unreal = commandlet
    material = FakeMaterial(used_with_nanite=False)
    mesh = FakeStaticMesh(enabled=False)

    result = module.enforce_nanite_material_policy(mesh, [material])

    assert result == {
        "material_blend_modes": ["BLEND_OPAQUE"],
        "nanite_policy": "eligible_static_opaque",
        "nanite_enabled": True,
    }
    assert mesh.settings.enabled is True
    assert material.used_with_nanite is True
    assert unreal.EditorAssetLibrary.saved == [material, mesh]


def test_unproven_opaque_material_fails_safe_to_non_nanite(commandlet) -> None:
    module, unreal = commandlet
    base = FakeMaterial(used_with_nanite=False, refuse_usage_edit=True)
    mesh = FakeStaticMesh(enabled=True)

    result = module.enforce_nanite_material_policy(mesh, [base])

    assert result == {
        "material_blend_modes": ["BLEND_OPAQUE"],
        "nanite_policy": "eligible_static_opaque",
        "nanite_enabled": False,
    }
    assert base.used_with_nanite is False
    assert unreal.EditorAssetLibrary.saved == [mesh]


def test_nonopaque_material_keeps_existing_disabled_policy(commandlet) -> None:
    module, unreal = commandlet
    material = FakeMaterial(
        used_with_nanite=False,
        blend_mode="BLEND_TRANSLUCENT",
    )
    mesh = FakeStaticMesh(enabled=True)

    result = module.enforce_nanite_material_policy(mesh, [material])

    assert result == {
        "material_blend_modes": ["BLEND_TRANSLUCENT"],
        "nanite_policy": "disabled_nonopaque_material",
        "nanite_enabled": False,
    }
    assert material.used_with_nanite is False
    assert unreal.EditorAssetLibrary.saved == [mesh]
