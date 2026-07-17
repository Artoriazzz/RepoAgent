from repo_agent.core.task_constraints import route_task_constraints


def test_nullable_pandas_profile_requires_issue_and_allowed_source_evidence(tmp_path):
    path = tmp_path / "sklearn" / "utils" / "multiclass.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "from .validation import check_array\n"
        "def type_of_target(y):\n"
        "    if is_multilabel(y):\n"
        "        return 'multilabel-indicator'\n",
        encoding="utf-8",
    )

    routing = route_task_constraints(
        problem_statement="Support nullable pandas extension dtypes.",
        cwd=tmp_path,
        allowed_ranges={"sklearn/utils/multiclass.py": [[1, 4]]},
    )

    assert routing.candidate_profiles == ("nullable_pandas",)
    assert [profile.name for profile in routing.active_profiles] == ["nullable_pandas"]
    assert routing.rejected_profiles == ()
    assert "matched_display_name" not in "\n".join(routing.prompts)


def test_matched_alias_profile_does_not_inject_nullable_constraint(tmp_path):
    path = tmp_path / "sphinx" / "ext" / "autodoc" / "typehints.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "def modify_field_list(annotations):\n"
        "    for name in annotations:\n"
        "        has_description = name in annotations\n",
        encoding="utf-8",
    )

    routing = route_task_constraints(
        problem_statement="Autodoc duplicates *args and **kwargs parameter fields.",
        cwd=tmp_path,
        allowed_ranges={"sphinx/ext/autodoc/typehints.py": [[1, 3]]},
    )

    assert routing.candidate_profiles == ("matched_alias",)
    assert [profile.name for profile in routing.active_profiles] == ["matched_alias"]
    assert routing.rejected_profiles == ()
    assert "_pandas_dtype_needs_early_conversion" not in "\n".join(routing.prompts)


def test_issue_candidate_is_rejected_when_allowed_source_does_not_confirm_it(tmp_path):
    path = tmp_path / "module.py"
    path.write_text("def unrelated(value):\n    return value\n", encoding="utf-8")

    routing = route_task_constraints(
        problem_statement="Autodoc duplicates *args and **kwargs parameter fields.",
        cwd=tmp_path,
        allowed_ranges={"module.py": [[1, 2]]},
    )

    assert routing.candidate_profiles == ("matched_alias",)
    assert routing.active_profiles == ()
    assert routing.rejected_profiles == ("matched_alias",)
    assert routing.to_dict()["injected_prompt_chars"] == 0


def test_unrelated_task_has_no_constraint_profile(tmp_path):
    path = tmp_path / "module.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")

    routing = route_task_constraints(
        problem_statement="Correct a warning message.",
        cwd=tmp_path,
        allowed_ranges={"module.py": [[1, 1]]},
    )

    assert routing.candidate_profiles == ()
    assert routing.active_profiles == ()
    assert routing.rejected_profiles == ()
