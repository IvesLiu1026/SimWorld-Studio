from __future__ import annotations

import ast
import hashlib
import pathlib
import sys
import types

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[2]
COMMANDLET = (
    ROOT / "tools/ue/vista_playable_home/verify_imported_nanite_commandlet.py"
)
NAMESPACE = "/Game/VISTA/PlayableHome/vista_playable_home_r1"


class FakeObject:
    def __init__(self, path: str, **properties) -> None:
        self.path = path
        self.properties = properties

    def get_path_name(self):
        return self.path

    def get_editor_property(self, name: str):
        if name not in self.properties:
            raise AttributeError(name)
        return self.properties[name]


class FakeMaterial(FakeObject):
    def get_base_material(self):
        return self


class FakeMaterialInstance(FakeObject):
    def get_base_material(self):
        current = self
        for _ in range(32):
            if isinstance(current, FakeMaterial):
                return current
            current = current.properties["parent"]
        raise RuntimeError("cycle")


class FakeStaticMesh(FakeObject):
    def has_valid_nanite_data(self):
        return self.properties["valid_nanite_data"]


class FakeSlot(FakeObject):
    pass


@pytest.fixture
def commandlet(monkeypatch: pytest.MonkeyPatch):
    unreal = types.ModuleType("unreal")
    unreal.Material = FakeMaterial
    unreal.MaterialInstanceConstant = FakeMaterialInstance
    unreal.StaticMesh = FakeStaticMesh
    unreal.MaterialUsage = types.SimpleNamespace(MATUSAGE_NANITE="nanite")
    unreal.MaterialEditingLibrary = types.SimpleNamespace(
        has_material_usage=lambda material, usage: (
            usage == "nanite" and material.properties["has_nanite_usage"]
        )
    )

    class EditorAssetLibrary:
        assets = {}

        @classmethod
        def does_asset_exist(cls, path):
            return path in cls.assets

        @classmethod
        def load_asset(cls, path):
            return cls.assets.get(path)

    unreal.EditorAssetLibrary = EditorAssetLibrary
    unreal.Paths = types.SimpleNamespace(get_project_file_path=lambda: "/tmp/project.uproject")
    unreal.log = lambda message: None
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
    module = types.ModuleType("vista_verify_imported_nanite_commandlet_test")
    module.__file__ = str(COMMANDLET)
    exec(compile(tree, str(COMMANDLET), "exec"), module.__dict__)
    return module, unreal


def test_private_material_paths_match_native_sha_contract(commandlet) -> None:
    module, _ = commandlet
    default_source = "/InterchangeAssets/gltf/M_Default.M_Default"
    opaque_ds_source = (
        "/InterchangeAssets/gltf/MaterialInstances/"
        "MI_Default_Opaque_DS.MI_Default_Opaque_DS"
    )

    assert hashlib.sha256(default_source.encode()).hexdigest()[:16] == "54bd2060f8fdb686"
    assert module.private_material_object_path(NAMESPACE, default_source) == (
        NAMESPACE + "/Internal/Materials/"
        "VISTA_M_Default_54bd2060f8fdb686.VISTA_M_Default_54bd2060f8fdb686"
    )
    assert hashlib.sha256(opaque_ds_source.encode()).hexdigest()[:16] == "a53f40f348e138a2"
    assert module.private_material_object_path(NAMESPACE, opaque_ds_source) == (
        NAMESPACE + "/Internal/Materials/VISTA_MI_Default_Opaque_DS_a53f40f348e138a2."
        "VISTA_MI_Default_Opaque_DS_a53f40f348e138a2"
    )


def test_parent_chain_is_cycle_free_bounded_and_private(commandlet) -> None:
    module, _ = commandlet
    root = FakeMaterial(
        NAMESPACE + "/Internal/Materials/VISTA_M_Root_0000.VISTA_M_Root_0000",
        blend_mode="BLEND_OPAQUE",
        used_with_nanite=True,
        has_nanite_usage=True,
    )
    parent = FakeMaterialInstance(
        NAMESPACE + "/Internal/Materials/VISTA_MI_Parent_0000.VISTA_MI_Parent_0000",
        parent=root,
    )
    slot = FakeMaterialInstance(NAMESPACE + "/Assets/A/MI.MI", parent=parent)

    chain, found_root = module.trace_material_parent_chain(slot, NAMESPACE)

    assert chain == [slot.path, parent.path, root.path]
    assert found_root is root


def test_parent_chain_rejects_cycle_and_external_root(commandlet) -> None:
    module, _ = commandlet
    cyclic = FakeMaterialInstance(NAMESPACE + "/Assets/A/MI.MI")
    cyclic.properties["parent"] = cyclic
    with pytest.raises(RuntimeError, match="cycle"):
        module.trace_material_parent_chain(cyclic, NAMESPACE)

    external = FakeMaterial(
        "/InterchangeAssets/gltf/M_Default.M_Default",
        blend_mode="BLEND_OPAQUE",
    )
    with pytest.raises(RuntimeError, match="outside"):
        module.trace_material_parent_chain(external, NAMESPACE)


def test_verify_reloads_mesh_and_checks_persisted_contract(commandlet) -> None:
    module, unreal = commandlet
    project = "/tmp/project.uproject"
    expected_default = module.private_material_object_path(
        NAMESPACE, module.PRIVATE_DEFAULT_SOURCE)
    expected_opaque_ds = module.private_material_object_path(
        NAMESPACE, module.PRIVATE_OPAQUE_DS_SOURCE)
    root = FakeMaterial(
        expected_default,
        blend_mode="BLEND_OPAQUE",
        used_with_nanite=True,
        has_nanite_usage=True,
    )
    private_instance = FakeMaterialInstance(expected_opaque_ds, parent=root)
    slot_instance = FakeMaterialInstance(
        NAMESPACE + "/Assets/A/Materials/MI_A.MI_A", parent=private_instance)
    slot = FakeSlot("slot", material_interface=slot_instance)
    mesh_path = NAMESPACE + "/Assets/A/A.A"
    settings = FakeObject("nanite", enabled=True)
    mesh = FakeStaticMesh(
        mesh_path,
        nanite_settings=settings,
        static_materials=[slot],
        valid_nanite_data=True,
    )
    shared = FakeMaterial(
        module.SHARED_DEFAULT_MATERIAL,
        blend_mode="BLEND_OPAQUE",
        used_with_nanite=False,
        has_nanite_usage=False,
    )
    unreal.EditorAssetLibrary.assets = {
        expected_default: root,
        expected_opaque_ds: private_instance,
        mesh_path: mesh,
        module.SHARED_DEFAULT_MATERIAL: shared,
    }
    receipt = {
        "content_namespace": NAMESPACE,
        "assets": [
            {
                "source_kind": "bundle",
                "object_path": mesh_path,
                "inspection": {
                    "object_path": mesh_path,
                    "material_paths": [slot_instance.path],
                    "material_blend_modes": ["BLEND_OPAQUE"],
                    "nanite_policy": "eligible_static_opaque",
                    "nanite_enabled": True,
                },
            },
            {"source_kind": "builtin", "object_path": "/Script/Fake"},
        ],
    }

    observations = module.verify_persisted_assets(receipt, project)

    assert [item["object_path"] for item in observations["meshes"]] == [mesh_path]
    assert observations["meshes"][0]["nanite_data_valid"] is True
    assert observations["meshes"][0]["material_chains"][0]["root_path"] == expected_default
    assert observations["material_roots"] == [{
        "object_path": expected_default,
        "used_with_nanite": True,
        "has_nanite_usage": True,
    }]
    assert observations["shared_default_material"]["used_with_nanite"] is False


def test_read_only_commandlet_has_no_mutation_or_save_calls() -> None:
    tree = ast.parse(COMMANDLET.read_text(encoding="utf-8"), filename=str(COMMANDLET))
    forbidden = {
        "delete_asset",
        "delete_directory",
        "make_directory",
        "rename_asset",
        "save_asset",
        "save_directory",
        "save_loaded_asset",
        "set_editor_property",
        "set_material_usage",
    }
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            calls.append(node.func.attr)
    assert forbidden.isdisjoint(calls)
    source = COMMANDLET.read_text(encoding="utf-8")
    assert "AssetTools" not in source
    assert "unreal.load_asset" not in source
    assert "EditorAssetLibrary.load_asset" in source
