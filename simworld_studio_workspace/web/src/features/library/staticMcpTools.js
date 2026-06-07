export const STATIC_MCP_TOOL_DEFS = [
  {
    id: "spawn_blueprint_actor",
    name: "spawn_blueprint_actor",
    mcpName: "spawn_blueprint_actor",
    enabled: true,
    description:
      "Spawn a SimWorld Blueprint actor (building, tree, vehicle, prop). Use this for all CityDatabase assets. The blueprint_id can be a full path like '/Game/CityDatabase/blueprints/BP_Building_01.BP_Building_01_C', or a shorthand like 'BP_Building_01', 'BP_Tree1', etc. For buildings you can even use just the number like '01' through '06'.",
    paramsSchema: {
      type: "object",
      properties: {
        actor_name: { type: "string", description: "Unique name for this actor (e.g. 'House_01', 'Tree_Left_1')" },
        blueprint_id: {
          type: "string",
          description:
            "Blueprint path or shorthand. Buildings: 'BP_Building_01' to 'BP_Building_06' (ONLY 01-06 available) (or just number). Trees: 'BP_Tree1'-'BP_Tree6'. Vehicles: 'BP_Scooter_01'-'BP_Scooter_04', 'BP_Cart'. Props: 'BP_Hydrant', 'BP_Trash_bin_a', 'BP_Table', etc.",
        },
        location: {
          type: "array",
          items: { type: "number" },
          description:
            "[x, y, z] in UE units (cm). 1m=100 units. Ground is 200m x 200m centered at origin, so keep X and Y between -9500 and 9500. Values outside this range will be clamped to stay on the ground.",
        },
        rotation: { type: "array", items: { type: "number" }, description: "[pitch, yaw, roll] in degrees" },
        scale: { type: "array", items: { type: "number" }, description: "[x, y, z] scale multipliers, default [1,1,1]" },
      },
      required: ["actor_name", "blueprint_id", "location"],
    },
  },
  {
    id: "spawn_actor",
    name: "spawn_actor",
    mcpName: "spawn_actor",
    enabled: true,
    description:
      "Spawn a static mesh actor. Use for basic shapes (/Engine/BasicShapes/Cube, Plane, etc.) or SM_ meshes. For SimWorld buildings/trees/props, prefer spawn_blueprint_actor instead.",
    paramsSchema: {
      type: "object",
      properties: {
        name: { type: "string", description: "Unique actor name" },
        static_mesh: {
          type: "string",
          description: "Full mesh path, e.g. '/Engine/BasicShapes/Cube.Cube' or '/Game/CityDatabase/meshes/SM_Road.SM_Road'",
        },
        location: { type: "array", items: { type: "number" }, description: "[x, y, z]" },
        rotation: { type: "array", items: { type: "number" }, description: "[pitch, yaw, roll]" },
        scale: { type: "array", items: { type: "number" }, description: "[x, y, z]" },
      },
      required: ["name", "static_mesh", "location"],
    },
  },
  { id: "delete_actor", name: "delete_actor", mcpName: "delete_actor", enabled: true, description: "Delete an actor by its name.", paramsSchema: { type: "object", properties: { name: { type: "string", description: "Actor name to delete" } }, required: ["name"] } },
  { id: "delete_all_spawned", name: "delete_all_spawned", mcpName: "delete_all_spawned", enabled: true, description: "Delete ALL actors spawned in this session. Use to clear the scene before rebuilding.", paramsSchema: { type: "object", properties: {} } },
  { id: "get_actors_in_level", name: "get_actors_in_level", mcpName: "get_actors_in_level", enabled: true, description: "List all actors currently in the UE level.", paramsSchema: { type: "object", properties: {} } },
  { id: "find_actors_by_name", name: "find_actors_by_name", mcpName: "find_actors_by_name", enabled: true, description: "Search for actors whose name matches a pattern.", paramsSchema: { type: "object", properties: { pattern: { type: "string", description: "Name pattern to search" } }, required: ["pattern"] } },
  { id: "set_actor_transform", name: "set_actor_transform", mcpName: "set_actor_transform", enabled: true, description: "Move, rotate, or scale an existing actor.", paramsSchema: { type: "object", properties: { name: { type: "string", description: "Actor name" }, location: { type: "array", items: { type: "number" }, description: "[x, y, z]" }, rotation: { type: "array", items: { type: "number" }, description: "[pitch, yaw, roll]" }, scale: { type: "array", items: { type: "number" }, description: "[x, y, z]" } }, required: ["name"] } },
  { id: "take_screenshot", name: "take_screenshot", mcpName: "take_screenshot", enabled: true, description: "Capture a screenshot of the current UE viewport and save it as PNG.", paramsSchema: { type: "object", properties: { filename: { type: "string", description: "Output filename (optional, auto-generated if omitted)" } } } },
  { id: "execute_python_script", name: "execute_python_script", mcpName: "execute_python_script", enabled: true, description: "Execute arbitrary Unreal Engine Python script. Use for advanced operations not covered by other tools.", paramsSchema: { type: "object", properties: { script: { type: "string", description: "Python code to execute in UE" } }, required: ["script"] } },
  { id: "list_assets", name: "list_assets", mcpName: "list_assets", enabled: true, description: "List available SimWorld assets. Returns buildings, trees, vehicles, street furniture, roads, and static meshes with their paths.", paramsSchema: { type: "object", properties: { category: { type: "string", description: "Optional: 'buildings', 'trees', 'vehicles', 'street_furniture', 'roads', 'static_meshes'. Omit for all." } } } },
  { id: "setup_environment", name: "setup_environment", mcpName: "setup_environment", enabled: true, description: "CALL THIS FIRST before spawning any objects! Sets up the scene environment: directional light (sun), sky atmosphere, sky light, fog, ground plane, and increases view distance. Without this, the scene will be black/empty.", paramsSchema: { type: "object", properties: { ground_size: { type: "number", description: "Ground plane scale (default 200 = 20km x 20km). Use 100 for small scenes, 300 for large cities." }, time_of_day: { type: "string", description: "'morning', 'noon', 'afternoon' (default), 'sunset', or 'night'" } } } },
  { id: "verify_scene", name: "verify_scene", mcpName: "verify_scene", enabled: true, description: "Call a verifier AI (Claude) to analyze the current scene. Takes a screenshot, gets all actors, then asks Claude to evaluate if placement is correct and matches the original request. Returns structured feedback with status (PASS/NEEDS_IMPROVEMENT/FAIL), issues found, and actionable suggestions. Use this after placing objects to check quality before finishing.", paramsSchema: { type: "object", properties: { original_request: { type: "string", description: "The original scene generation request to verify against (e.g. 'a suburban street with 3 houses and 2 trees')" }, focus_areas: { type: "string", description: "Optional: specific aspects to focus on (e.g. 'check building spacing', 'verify tree placement')" } }, required: [] } },
].sort((a, b) => a.id.localeCompare(b.id));
