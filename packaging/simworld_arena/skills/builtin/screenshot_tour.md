---
id: screenshot_tour
name: Camera & Screenshot Guide
version: 2.0.0
author: simworld-team
tags: [camera, screenshot, capture, viewpoint]
dependencies: []
description: >
  Position the viewport camera and capture screenshots from multiple
  angles using set_camera and take_screenshot tools.
---

# Camera & Screenshot Guide

## Overview
Use `set_camera` to position the viewport and `take_screenshot` to capture
images. Combine for multi-angle documentation of scenes.

## Setting Camera Position
```
Tool: set_camera
  location: [x, y, z]
  rotation: [pitch, yaw, roll]
```

## Taking Screenshots
```
Tool: take_screenshot
  filename: "my_shot.png"
```
Screenshots are saved to the `tmp/screens/` directory.

## Camera Rotation Guide
- **pitch**: Up/down angle
  - `-90`: Looking straight down (top-down view)
  - `-45`: 45-degree aerial view
  - `-15`: Slight downward look (street level)
  - `0`: Horizontal (eye level)
- **yaw**: Horizontal rotation
  - `0`: Facing +X (east)
  - `90`: Facing +Y (north)
  - `180`: Facing -X (west)
  - `270`: Facing -Y (south)
- **roll**: Always 0 (no tilt)

## Standard Viewpoints

### Top-Down Overview
```
set_camera: location=[0, 0, 15000], rotation=[-90, 0, 0]
```

### Aerial 45-Degree
```
set_camera: location=[-5000, -5000, 8000], rotation=[-45, 45, 0]
```

### Street Level (eye height)
```
set_camera: location=[500, 300, 170], rotation=[-5, 90, 0]
```

### Dramatic Low Angle
```
set_camera: location=[2000, 0, 100], rotation=[10, 180, 0]
```

## Multi-Angle Tour Pattern
Capture a scene from 4+ angles for complete documentation:

1. **Aerial overview**: High altitude, looking down
2. **Front view**: Eye level, facing the main subject
3. **Side view**: 90 degrees from front
4. **Detail shot**: Close up on interesting elements

## Distance Guide by Scene Type

| Scene | Camera Altitude | Distance from Center |
|-------|----------------|---------------------|
| Single building (small) | 3000-5000 | 3000-5000 |
| Single building (tall) | 10000-20000 | 10000-15000 |
| City block | 8000-15000 | 5000-10000 |
| Neighborhood | 15000-25000 | 10000-15000 |
| Full city | 30000-50000 | 20000+ |

## Tips
- Take screenshots AFTER scene is fully built (not during spawning)
- Wait 500ms+ after camera move before screenshot for rendering
- Use pitch=-90 for clean top-down maps
- Combine altitude and pitch for different perspectives
- Name screenshots descriptively: "aerial_overview.png", "street_north.png"
