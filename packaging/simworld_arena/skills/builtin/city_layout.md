---
id: city_layout
name: City Block Layout
version: 2.0.0
author: simworld-team
tags: [city, layout, planning, buildings, roads]
dependencies: []
description: >
  Plan and build city blocks with buildings, roads, and trees using
  MCP tools. Covers grid layouts, organic neighborhoods, and downtown areas.
---

# City Block Layout

## Overview
Use `spawn_blueprint_actor` to place buildings and roads in organized city blocks.
Always call `setup_environment` first for lighting, then build layouts systematically.

## Building Assets
- **Residential** (small, 01-09): `BP_Building_01` to `BP_Building_09`
- **Commercial** (medium, 10-30): `BP_Building_10` to `BP_Building_30`
- **Skyscrapers** (tall, 31+): `BP_Building_31` to `BP_Building_127`

Blueprint path format: `/Game/CityDatabase/blueprints/BP_Building_XX.BP_Building_XX_C`

## Layout Patterns

### Grid Block (4 buildings around a center)
```
spawn_blueprint_actor: BP_Building_01 at (-1500, -1500, 0)
spawn_blueprint_actor: BP_Building_02 at (1500, -1500, 0)
spawn_blueprint_actor: BP_Building_03 at (-1500, 1500, 0)
spawn_blueprint_actor: BP_Building_04 at (1500, 1500, 0)
```

### Street-Facing Row
Place buildings along one axis with consistent spacing:
- Small residential: spacing ~3000 units apart
- Medium commercial: spacing ~5000 units apart
- Skyscrapers: spacing ~8000-12000 units apart

### Neighborhood Cluster
1. Place 4-6 small buildings (01-09) in a rough grid
2. Add trees between buildings (BP_Tree1 through BP_Tree6)
3. Add a road segment along one edge

## Camera Recommendations
- **Overview**: altitude 8000-15000, pitch -60 to -90
- **Street level**: altitude 300-500, pitch -5 to -15
- **Small buildings** (01-09): camera at 3000-8000 altitude
- **Skyscrapers** (31+): camera at 15000-40000 altitude

## Tips
- Always `setup_environment` before placing anything
- Use `delete_all_spawned` to clear the scene
- Small buildings (01-09) are ~2000 units, skyscrapers can be 30,000+ units
- Rotate buildings with yaw (0, 90, 180, 270) to face streets
- Set `ld_max_draw_distance=0` on actors to prevent culling at distance
