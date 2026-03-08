---
id: building_placement
name: Building Placement & Spacing
version: 2.0.0
author: simworld-team
tags: [buildings, placement, spacing, architecture]
dependencies: []
description: >
  Detailed guide for placing individual buildings with correct spacing,
  rotation, and scale. Covers all 127 building types and their sizes.
---

# Building Placement & Spacing

## Overview
Each building blueprint has different dimensions. Proper spacing prevents
overlapping and creates realistic urban layouts.

## Spawning a Building
```
Tool: spawn_blueprint_actor
  actor_name: "my_building"
  blueprint_name: "/Game/CityDatabase/blueprints/BP_Building_01.BP_Building_01_C"
  location: [x, y, z]
  rotation: [0, yaw, 0]
```

## Building Size Categories

| Range | Type | Approx Height | Spacing |
|-------|------|---------------|---------|
| 01-09 | Small residential | 1500-2500 | 2500-3500 |
| 10-30 | Medium commercial | 3000-9000 | 4000-6000 |
| 31-70 | Office towers | 8000-20000 | 6000-10000 |
| 71-127 | Skyscrapers | 15000-38000 | 10000-15000 |

## Rotation Guide
- `yaw: 0` — faces +X direction
- `yaw: 90` — faces +Y direction
- `yaw: 180` — faces -X direction
- `yaw: 270` — faces -Y direction
- For street-facing rows, align yaw perpendicular to the street

## Common Patterns

### L-Shaped Block
```
Building A at (0, 0, 0) yaw=0
Building B at (3000, 0, 0) yaw=0
Building C at (0, 3000, 0) yaw=90
```

### Mixed-Use Block
Place tall commercial in the center, small residential around edges:
```
Center: BP_Building_35 at (0, 0, 0)
Edges:  BP_Building_01-09 at offsets of 5000-8000
```

### Dense Downtown
Use skyscrapers (71+) with 10000+ unit spacing:
```
BP_Building_75 at (0, 0, 0)
BP_Building_80 at (12000, 0, 0)
BP_Building_85 at (0, 12000, 0)
BP_Building_90 at (12000, 12000, 0)
```

## Tips
- Always check building bounds with `get_actors_in_level` after spawning
- Z=0 is ground level — all buildings should be placed at z=0
- Use unique `actor_name` for each building to manage them later
- Combine with trees and street furniture for realism
