---
id: agent_face_and_approach
name: Agent Face and Approach Target
version: 1.0.0
author: simworld
tags: [agent, movement, navigation, rotation, facing, approach]
dependencies: [agent_control]
description: Compute and apply the correct yaw rotation for an agent to face another agent, then move toward it. Based on UE left-handed coordinate system — verified against real agent data.
---

## Agent Face and Approach Target Skill

This skill enables an agent to turn and face another agent, then walk toward it, using the SimWorld agent control API.

---

### Coordinate System Reference

Unreal Engine uses a left-handed coordinate system:

| Yaw | Facing direction |
|-----|-----------------|
| 0°  | +X (East)       |
| 90° | +Y (South)      |
| -90° | -Y (North)     |
| 180° | -X (West)      |

`get_agent_state` returns `rotation: [pitch, yaw, roll]` where yaw ∈ (-180°, 180°].

> Verified: Agent1 (-29109.5, -31348.6) → Agent2 (368, 805), ΔX=29477.5, ΔY=32153.6  
> `atan2(32153.6, 29477.5) ≈ 49°` ✅ matches observed required yaw = 49°

---

### Core Formula

To compute the yaw an agent must have to face a target:

```python
import math

def compute_facing_yaw(self_loc, target_loc):
    dx = target_loc[0] - self_loc[0]
    dy = target_loc[1] - self_loc[1]
    return math.degrees(math.atan2(dy, dx))   # returns (-180, 180]
```

To convert that into a **relative rotation delta** for `agent_rotate`:

```python
def compute_rotate_delta(current_yaw, target_yaw):
    delta = target_yaw - current_yaw
    # Normalize to (-180, 180]
    delta = (delta + 180) % 360 - 180
    direction = "right" if delta >= 0 else "left"
    angle = abs(delta)
    return angle, direction
```

---

### Step-by-Step: Face Another Agent

```
1. get_agent_state(agent_name="Pedestrian_1")
   # → { location: [x1, y1, z1], rotation: [pitch, yaw1, roll] }

2. get_agent_state(agent_name="Pedestrian_2")
   # → { location: [x2, y2, z2], rotation: [...] }

3. # Compute in Python:
   target_yaw = atan2(y2 - y1, x2 - x1)   # degrees
   delta      = normalize(target_yaw - yaw1)
   direction  = "right" if delta >= 0 else "left"
   angle      = abs(delta)

4. agent_rotate(agent_name="Pedestrian_1", angle=<angle>, direction=<direction>, agent_type="pedestrian")

5. get_agent_state(agent_name="Pedestrian_1")   # Verify new yaw ≈ target_yaw
```

---

### Step-by-Step: Approach Another Agent

```
1. get_agent_state(agent_name="Pedestrian_1")
   get_agent_state(agent_name="Pedestrian_2")

2. # Face target first (see above)
   agent_rotate(agent_name="Pedestrian_1", angle=<angle>, direction=<direction>, agent_type="pedestrian")

3. # Estimate duration from distance and speed
   dist     = sqrt((x2-x1)^2 + (y2-y1)^2)
   speed    = 200          # UE units/s (normal walk)
   duration = dist / speed * 0.85   # 0.85 buffer — stop slightly before target

4. agent_set_speed(agent_name="Pedestrian_1", speed=200)
   agent_step_forward(agent_name="Pedestrian_1", duration=<duration>)

5. get_agent_state(agent_name="Pedestrian_1")   # Verify new position
   take_screenshot()                             # Visual confirmation
```

---

### Speed Reference for Duration Estimation

| Speed value | Style       | UE units/s |
|-------------|-------------|------------|
| 100         | Slow walk   | ~100       |
| 200         | Normal walk | ~200       |
| 400         | Running     | ~400       |

Estimated duration formula:
```
duration = distance / speed * 0.85
```
The 0.85 factor prevents overshooting. Adjust down (0.7) for tight spaces.

---

### Full Example: Pedestrian_1 Walks to Pedestrian_2

```
1. get_agent_state(agent_name="Pedestrian_1")
   # → location: [-29109, -31348, 90], rotation: [0, -90, 0]

2. get_agent_state(agent_name="Pedestrian_2")
   # → location: [368, 805, 90]

3. # target_yaw = atan2(805-(-31348), 368-(-29109)) = atan2(32153, 29477) ≈ 49°
   # current_yaw = -90°
   # delta = 49 - (-90) = 139°  → turn RIGHT 139°

4. agent_rotate(agent_name="Pedestrian_1", angle=139, direction="right", agent_type="pedestrian")

5. # dist ≈ sqrt(29477² + 32153²) ≈ 43600 units
   # duration = 43600 / 200 * 0.85 ≈ 185s  (long distance — consider waypoints instead)

6. agent_set_speed(agent_name="Pedestrian_1", speed=200)
   agent_step_forward(agent_name="Pedestrian_1", duration=185)

7. get_agent_state(agent_name="Pedestrian_1")
   take_screenshot()
```

---

### Alternative: Use Path Following for Long Distances

For large distances, prefer `agent_set_path` over a single `agent_step_forward`:

```
agent_set_speed(agent_name="Pedestrian_1", speed=200)
agent_set_path(agent_name="Pedestrian_1", waypoints=[[<x2>, <y2>]])
```

This lets the navigation system handle the path, avoiding obstacles and drift.

---

### Common Pitfalls

| Pitfall | Rule |
|---------|------|
| `atan2` argument order | Always `atan2(dy, dx)` — never reversed |
| Yaw delta normalization | Normalize delta to (-180°, 180°] before choosing direction |
| Pitch and Roll | Never modify — keep at 0 |
| Duration overshooting | Use 0.85× multiplier; reduce to 0.7 in tight spaces |
| Long distances | Use `agent_set_path` instead of a single `agent_step_forward` |
| Moving target | Re-run steps 1–4 each tick to keep tracking |