# Workflow Image Generation

Method: built-in `image_gen`, followed by one targeted connection correction.
Selected asset: `repair-workflow-v5.png`.
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

## Readability Redesign

Method: built-in `image_gen` edit of `repair-workflow-v2.png`.
Final asset: `repair-workflow-v3.png`. Earlier versions are preserved.

Redesign the workflow for a GitHub README displayed at approximately 900 CSS
pixels wide. Use a 3:2 landscape canvas, three horizontal lanes with at most
four nodes across, large Chinese primary labels and readable English role
names beneath. Wrap long role names into two lines. Reduce the heading and
unused whitespace. Keep a clean white background with blue coordination,
green memory/success and amber failure routes.

Place Issue, MemoryQueryAgent and retrieval in the first lane; Coordinator,
Initial Implement and Harness in the second; FailureReview, Corrective
Implement and the second Harness in the third. Share ExperienceCurator at
the right. Put the budget decision and termination below. Preserve all
directed edges from the original graph, especially initial failure into
FailureReview, budget remaining back into Coordinator, and dashed experience
reuse into retrieval. Do not invent additional nodes or connections.

## Scope Routing Correction (Current)

This version supersedes the earlier routing specifications above.
Method: built-in `image_gen`, with two targeted edge-cleanup edits.
Final asset: `repair-workflow-v4.png`.

```text
Edit the supplied RepoAgent workflow infographic to CORRECT the control flow. Keep its readable large Chinese primary labels and smaller English role names, clean white background, blue work routes, amber failures/decisions, green success/memory. Landscape about 3:2; compact polished orthogonal diagram, maximum four nodes per row. You may fully rearrange nodes to avoid crossings. Typography must remain legible at 900px README width. Title modest "RepoAgent · 修复流程".

Use EXACTLY this graph. It replaces the old graph:
A 公开 Issue -> B 检索查询提取 / MemoryQueryAgent -> C 经验检索 / 全局 + 仓库经验 -> D 定位与决策 / Coordinator -> E 首次修复 / Initial Implement -> V 独立验证 / Harness.
V -> G 沉淀经验 / ExperienceCurator, green edge "通过".
G -> L 结束任务, green edge. This terminal edge is REQUIRED.
V -> K diamond "预算耗尽？", amber edge "未通过".
K -> L, edge "是".
K -> H 失败归因 / FailureReview, edge "否".
H -> R diamond "需要扩大修改范围？".
R -> D Coordinator, blue edge "是 · 重新规划". Only this scope-expansion decision can route back to Coordinator.
R -> I 定向纠正 / Corrective Implement, edge "否".
I -> V 独立验证 / Harness, blue loop. Reuse the SAME V node, do not add another validation node.
A thin optional dashed green G -> C "后续任务复用" denotes reuse across later tasks, not current-task execution; omit this dashed cross-task edge if it makes the required solid G->L terminal edge unclear.

Non-negotiable: FailureReview MUST lead to an explicit scope-routing diamond; within current authorized scope -> Corrective Implement directly, expanded scope -> Coordinator replan. Budget available NEVER goes directly to Coordinator. Independent validation is shared by initial and corrective patches. Successful experience curation MUST end the task. Exhausted budget ends task too. Do not write 再次验证. Do not introduce extra roles. Display every decision's 是/否 branches and arrowheads clearly. Do not use a direct FailureReview->Coordinator edge bypassing scope decision. Do not leave any node disconnected. Use short wrapped text in decision diamonds, large type. Favor graph correctness and readability over decorative icons. All text Chinese and English accurately rendered.
```

Targeted edits: the scope diamond's YES branch must originate at its bottom
point and return to Coordinator via an outer bottom/right route. Keep this
route clear of the Corrective Implement rectangle. Its NO branch enters
Corrective Implement; that node has only one outgoing edge, into the shared
Harness. The final image omits the optional cross-task memory reuse edge to
keep task termination clear. Both successful curation and exhausted budget
lead to End Task.

## Coordinator Return Entry (Current Asset)

Method: built-in `image_gen` edit of `repair-workflow-v4.png`.
Final asset: `repair-workflow-v5.png`.

Preserve the graph, labels and layout. Extend the white right margin and
reroute the scope-expansion YES return edge outside all nodes. Replace the
arrowhead at Coordinator's bottom-right corner with a horizontal,
left-pointing arrowhead entering the midpoint of Coordinator's right side.
Keep its source at the scope diamond's bottom tip and retain the label
是 · 重新规划. Do not connect this edge to either implementation node.
