# Workflow Image Generation

Method: built-in `image_gen`, followed by one targeted connection correction.
Selected asset: `repair-workflow-v2.png`.
The editable earlier diagram remains in `repair-workflow.svg`.

## Generation Prompt

Use case: infographic-diagram.
Create a polished, publication-quality horizontal workflow infographic for the GitHub README of RepoAgent, a multi-agent repository repair platform. This is a brand NEW diagram design, not an edit. Landscape panoramic composition, about 2.6:1 aspect ratio, high-resolution crisp raster, generous margins. Make it sophisticated and genuinely well designed: clean white background, charcoal typography, deep emerald/teal for memory/success and cobalt for coordination/retry, restrained very light tints, fine lines, perfectly consistent spacing and rounded rectangles, clear hierarchy and immaculate readable Chinese + English labels. Tiny simple line icons are optional. No decorative illustrations, no robots, no gradients, no glow, no 3D, no dark background, no purple. Use readable large type, not a dense wiring diagram.

Title at top left: "RepoAgent". Small subtitle: "Memory-Augmented Repair Loop".
Lay out a calm main horizontal pipeline left to right in the upper portion. Place the failure/corrective path in a neatly aligned lower lane. Budget decision and termination occupy the lower left. All connections must be unambiguous, arrows routed with deliberate generous whitespace; avoid line crossings, lines over text, and huge border-hugging arrows. The success/experience node can sit at the far right spanning the lanes.

EXACT NODES AND LABELS (verbatim; English labels are the role names, Chinese smaller subtitles):
A "公开 Issue"
B "MemoryQueryAgent" / "提取问题与场景"
C "经验检索" / "全局 + 仓库经验"
D "Coordinator" / "定位 · 决策 · 授权"
E "Initial Implement" / "首次修复"
F "Harness" / "独立验证"
G "ExperienceCurator" / "抽象 · 合并 · 保存"
H "FailureReview" / "失败归因"
I "Corrective Implement" / "定向纠正"
J "Harness" / "再次验证"
K diamond decision "预算耗尽？"
L "结束任务"

EXACT DIRECTED GRAPH:
Upper pipeline A -> B -> C -> D -> E -> F.
F -> G, label "通过", green success.
F -> H, label "未通过".
Lower correction lane H -> I -> J.
J -> G, label "通过", green success.
J -> K, label "未通过".
K -> L, label "是".
K -> D, label "否 · 重新协调", blue prominent retry arrow pointing BACK into Coordinator. This return arrow is mandatory and must not point to another role.
G -> C, label "经验复用", a thin subtle dashed memory feedback arrow. This is for subsequent tasks.
The two Harness nodes are distinct: first validation and corrective validation.
Do not add, omit or invent nodes, text, metrics or connections. Render all text correctly, keep every node comfortably readable at README width. Prioritize compositional beauty and clear causal flow over visual embellishment.

## Targeted Correction

Preserve all labels, nodes, positions and styling. Replace the incorrect upper
Harness to Corrective Implement failure edge with an orthogonal orange arrow
from the upper Harness into FailureReview, labelled 未通过. Keep the existing
FailureReview to Corrective Implement to lower Harness sequence, both success
paths, memory reuse connection, budget termination and return to Coordinator.
There must be no direct upper Harness to Corrective Implement connection.
