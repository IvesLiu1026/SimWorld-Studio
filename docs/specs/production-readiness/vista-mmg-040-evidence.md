# VISTA `mmg_040` Import Evidence

Checked: 2026-07-14  
Purpose: bounded golden fixture for the `vista-simworld-scene/v1` importer. This evidence is reconstruction-only and must not be used as restricted assist-step prediction input.

## Identity disambiguation

The selected sample is the office high-shelf retrieval case:

- dataset revision: `round1_reviewed_latest`
- visual id: `mmg_040`
- case scope: `multimodal_grounded_safety_040`
- video model: `sora2`
- selected attempt: `7`
- selected scenario row: `multimodal_grounded`

This is not the different `sora2wd:remaining581:multimodal_grounded_safety_040` sandbox sample, which also contains `040` in its identifier.

## Source-backed claims

| Claim | VISTA source | Confidence | Remaining gap |
| --- | --- | --- | --- |
| The no-oracle handoff selects `mmg_040`, Sora2 attempt 7. | `for_senior_no_oracle/vista_assist_step_inputs_no_oracle.jsonl`, row id `mmg_040__multimodal_grounded::multimodal_grounded_safety_040::sora2::attempt_007` | High | The row does not carry media duration. |
| Attempt 7 is completed, saved, and selected for export. | `benchmarks/vista_mm_dialogue_exp/runs/top10_teacher_camera_desc_20260426/mmg_011_040_sora2/cases/multimodal_grounded_safety_040/pipeline_v2/media/video_attempts.json` | High | Reviewer identity and review notes are intentionally excluded from the fixture. |
| The reconstruction script specifies a 12-second office scene and beats at 0, 2, 5, and 9 seconds. | Same case, `pipeline_v2/media/render_script.yaml` | High | Runtime action support is checked separately by the timeline compiler. |
| The selected attempt's MP4 is 9,393,745 bytes with SHA-256 `e84d294e0ff86b41760e221100e29ff0d43ddbe84cd1d534c42f36e9f189d49f`; its sanitized media summary records 1280×720 and 12 seconds. | Same case, `pipeline_v2/media/attempts/attempt_007/video.mp4` and `media_summary.json`; checksum and byte count were recomputed read-only on 2026-07-14. | High for identity/checksum/declared duration | The media binary is deliberately not copied into the fixture, so import records its checksum rather than re-reading the binary. Production media storage still needs a verifier adapter. |
| The selected no-oracle dialogue asks for the high box and contrasts the chair with a ladder. | The exact no-oracle row above; cross-checked against `docs/project_management/scenario_pairing_manifest_2026-05-07/scenario_pairing_manifest.tsv` | High | Dialogue is context only; it must not be treated as an action or oracle label. |

## Fixture policy

The checked-in fixture may contain only the minimum render fields required for reconstruction, the selected no-oracle dialogue fields, relative source roles, and content checksums. It must not contain:

- absolute server paths;
- signed object-storage URLs or credentials;
- reviewer names, e-mail addresses, or review notes;
- prediction labels, assistance targets, visible-evidence atoms, or model outputs;
- images or video binaries.

The fixture therefore carries a reconstruction-only `vista-media-reference/v1` descriptor with a logical relative reference, content hash, byte count, duration, and dimensions. `integrity_status=recorded_checksum` is intentionally narrower than claiming the external binary was reverified during every import.

The importer must label render-derived environment, entity, camera, and timeline fields as `reconstruction_only`. Its evaluation-safe export must omit those fields entirely.
