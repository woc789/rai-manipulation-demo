# Safety-cone source asset (not yet integrated into runtime)

- Creator: Kenney.
- Pack: City Kit (Roads), 2.2.
- Source: https://kenney.nl/assets/city-kit-roads
- License: CC0; original `License.txt` included.
- Selected model: `construction-cone` in FBX and GLB, with the FBX palette texture.
- Downloaded pack size: approximately 2.7 MiB. Only the selected model files are retained here.
- Original ZIP SHA-256: see the obstacle integration note in `docs/SAFETY_CONE_INTEGRATION.md`.

## Intended use

A small tabletop safety cone marking a restricted transfer region, replacing the
central return crate. Proposed initial physical dimensions: roughly 8 cm base
width and 12 cm height, subject to mesh inspection and planning-only verification.
The final position must avoid the stock grasp/approach columns, receiving-bin
openings and camera lines of sight. No location is claimed safe before checking.

## Integration blocker

The current machine runs the exported O3DE GameLauncher and bundled product assets
(`Cache/linux/engine.pak`). No AssetProcessor, AssetProcessorBatch, AssetBuilder or
Editor installation was found in the inspected project/home/opt/local locations.
The ROS spawn service instantiates registered compiled spawnables, not arbitrary
FBX/GLB files. Adding these source files alone does not add a spawnable.

Do not change `demo_scene.yaml` to reference this model until the matching O3DE
asset pipeline has built the mesh/material and prefab/spawnable and registered
them with the runtime asset catalog. Do not replace only the MoveIt obstacle:
visible geometry, simulator collision and MoveIt geometry must agree.

No existing scene, database, pending order, engine archive or collision check was
modified by downloading these source assets.
