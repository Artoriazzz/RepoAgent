"""导出当前模式一的提示词正文，动态任务数据使用占位符，不发起模型请求。"""

from pathlib import Path

from repo_agent.core import subagent
from repo_agent.core.coordinator import coordinator_system_prompt
from repo_agent.core.experience import (
    RepairRunMode,
    build_experience_curator_prompt,
    build_failure_review_prompt,
)
from repo_agent.core.subagent import LineRange, WorkerConstraints
from repo_agent.core.llm import OUTPUT_LIMIT_RECOVERY_PROMPT


def main():
    output = Path(__file__).resolve().parents[1] / "docs" / "prompts"
    output.mkdir(parents=True, exist_ok=True)

    def save(name, sections):
        text = "# " + name + "\n\n"
        text += "由当前代码导出。花括号占位符表示运行时数据，不是额外的模型知识。\n"
        for heading, body in sections:
            text += "\n## " + heading + "\n\n" + body.strip() + "\n"
        (output / (name + ".md")).write_text(text, encoding="utf-8")

    save("Coordinator", [("System", coordinator_system_prompt(
        swebench_profile=True, repair_mode=RepairRunMode.EXPERIENCE_BUILD)),
        ("User", "## Instance\n{instance_id}\n\n## Repository\n{repo_dir}\n\n## Problem Statement\n{problem_statement}"),
        ("Budget Recovery (only after output limit)", OUTPUT_LIMIT_RECOVERY_PROMPT)])
    save("Implement", [("System - Base", subagent.SWEBENCH_IMPLEMENT_PROMPT),
        ("System - Initial Phase", subagent.SWEBENCH_EXPERIENCE_INITIAL_IMPLEMENT_PROMPT),
        ("System - Corrective Phase (replaces initial phase)", subagent.SWEBENCH_EXPERIENCE_CORRECTIVE_IMPLEMENT_PROMPT),
        ("User Wrapper", subagent._worker_prompt("{description}", "{self_contained_edit_brief}",
            WorkerConstraints(allowed_files=("{path}",), allowed_ranges={"{path}": (LineRange(1, 20),)}),
            output_schema=subagent.SWEBENCH_IMPLEMENT_OUTPUT_SCHEMA))])
    save("FailureReview", [("System", subagent.FAILURE_REVIEW_PROMPT),
        ("Shared User Wrapper", subagent._worker_prompt("{description}", "{review_task_below}")),
        ("User Template", build_failure_review_prompt(task="{public_task}", failed_patch="{failed_patch}",
            failure_log="{failure_log}", validation="{validation_summary}",
            implementation_report={"changed_decision": "{changed_decision}",
                                   "preserve_cases": ["{preserve_cases}"], "known_risks": ["{known_risks}"]}))])
    save("ExperienceCurator", [("System", subagent.EXPERIENCE_CURATOR_PROMPT),
        ("Shared User Wrapper", subagent._worker_prompt("{description}", "{curation_task_below}")),
        ("User Template", build_experience_curator_prompt(task="{public_task}",
            successful_patch="{successful_patch}", validation="{independent_validation}",
            provenance="{provenance}", failed_patch="{failed_patch}", feedback={"feedback": "{failure_feedback}"}))])
    print(output)


if __name__ == "__main__":
    main()
