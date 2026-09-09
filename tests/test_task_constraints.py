from repo_agent.core.task_constraints import (
    PROFILES,
    derive_profile_context_ranges,
    derive_profile_edit_ranges,
    profile_patch_contract_issues,
    route_task_constraints,
)


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


def test_experience_profiles_are_phase_isolated_and_source_confirmed(tmp_path):
    path = tmp_path / "lib" / "matplotlib" / "widgets.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "def set_val(self, val):\n"
        "    xy = self.poly.xy\n"
        "    self._handles[0].set_xdata([val[0]])\n",
        encoding="utf-8",
    )
    kwargs = {
        "problem_statement": "RangeSlider cannot initialize valinit.",
        "cwd": tmp_path,
        "allowed_ranges": {"lib/matplotlib/widgets.py": [[1, 3]]},
    }

    benchmark = route_task_constraints(**kwargs, phase="benchmark_initial")
    experience = route_task_constraints(**kwargs, phase="experience_initial")

    assert benchmark.candidate_profiles == ()
    assert [profile.name for profile in experience.active_profiles] == [
        "mutable_state_lifecycle"
    ]
    assert "update both existing handles" in experience.prompts[0]


def test_relation_and_generated_name_profiles_use_semantic_contracts(tmp_path):
    inspectdb = tmp_path / "django" / "core" / "management" / "commands" / "inspectdb.py"
    inspectdb.parent.mkdir(parents=True)
    inspectdb.write_text("rel_to = table\natt_name = column\nextra_params = {}\n", encoding="utf-8")
    generated = route_task_constraints(
        problem_statement="inspectdb should generate related_name for duplicate relations",
        cwd=tmp_path,
        allowed_ranges={"django/core/management/commands/inspectdb.py": [[1, 3]]},
        phase="experience_initial",
    )

    checks = tmp_path / "django" / "contrib" / "admin" / "checks.py"
    checks.parent.mkdir(parents=True)
    checks.write_text(
        "isinstance(field, models.ManyToManyField)\nfield.one_to_many\nadmin.E109\n",
        encoding="utf-8",
    )
    relations = route_task_constraints(
        problem_statement="Error E108 misses reverse list_display choice relations",
        cwd=tmp_path,
        allowed_ranges={"django/contrib/admin/checks.py": [[1, 3]]},
        phase="experience_initial",
    )

    assert [profile.name for profile in generated.active_profiles] == [
        "generated_relation_name"
    ]
    assert "table2model(table_name)" in generated.prompts[0]
    assert [profile.name for profile in relations.active_profiles] == [
        "relation_protocol"
    ]
    assert "reverse M2M" in relations.prompts[0]


def test_relation_profile_accepts_clean_legacy_many_to_one_source(tmp_path):
    checks = tmp_path / "django" / "contrib" / "admin" / "checks.py"
    checks.parent.mkdir(parents=True)
    checks.write_text(
        "isinstance(field, models.ManyToManyField)\nfield.rel.field.many_to_one\nadmin.E109\n",
        encoding="utf-8",
    )

    routing = route_task_constraints(
        problem_statement="Error E108 misses reverse list_display choice relations",
        cwd=tmp_path,
        allowed_ranges={"django/contrib/admin/checks.py": [[1, 3]]},
        phase="experience_initial",
    )

    assert [profile.name for profile in routing.active_profiles] == ["relation_protocol"]


def test_narrow_range_activates_mutable_state_profile_without_handle_initializer(tmp_path):
    path = tmp_path / "lib" / "matplotlib" / "widgets.py"
    path.parent.mkdir(parents=True)
    path.write_text("def set_val(self, val):\n    xy = self.poly.xy\n", encoding="utf-8")

    routing = route_task_constraints(
        problem_statement="RangeSlider cannot initialize valinit.",
        cwd=tmp_path,
        allowed_ranges={"lib/matplotlib/widgets.py": [[1, 2]]},
        phase="experience_initial",
    )

    assert [profile.name for profile in routing.active_profiles] == ["mutable_state_lifecycle"]


def test_profile_patch_contracts_reject_v40_failure_shapes():
    generated = """\
+if sum(1 for _, ref_table in relations.values() if ref_table == ref_db_table) > 1:
+    extra_params["related_name"] = "%%(class)s_%s" % att_name
"""
    relation = """\
-getattr(field, "rel", None) and field.rel.field.many_to_one
+if getattr(field, "rel", None) is not None or getattr(field, "one_to_many", False):
"""
    mutable = """\
-xy[4] = val[0], .25
"""
    formatting = """\
+except ValueError:
+    g_sig_digits = 3
"""

    assert profile_patch_contract_issues(generated, ["generated_relation_name"])
    assert profile_patch_contract_issues(relation, ["relation_protocol"])
    assert profile_patch_contract_issues(mutable, ["mutable_state_lifecycle"])
    assert profile_patch_contract_issues(formatting, ["noninvertible_format_protocol"])


def test_profile_patch_contracts_accept_semantically_complete_shapes():
    generated = """\
+used_relations = set()
+if rel_to in used_relations:
+    extra_params["related_name"] = "%s_%s_set" % (table2model(table_name).lower(), att_name)
+used_relations.add(rel_to)
"""
    relation = """\
-if isinstance(field, models.ManyToManyField) or (getattr(field, "rel", None) and field.rel.field.many_to_one):
+if (field.is_relation and (field.many_to_many or field.one_to_many)) or (getattr(field, "rel", None) and field.rel.field.many_to_one):
"""
    mutable = """\
-self.poly = ax.axvspan(val[0], val[1])
+self.poly = ax.axvspan(val[0], val[1], closed=False)
-xy[4] = val[0], .25
+self._handles[0].set_xdata([val[0]])
+self._handles[1].set_xdata([val[1]])
+self._handles[0].set_ydata([val[0]])
+self._handles[1].set_ydata([val[1]])
"""
    formatting = """\
+if isinstance(self.norm, mpl.colors.BoundaryNorm):
+    delta = np.diff(self.norm.boundaries).max()
"""

    assert profile_patch_contract_issues(generated, ["generated_relation_name"]) == []
    assert profile_patch_contract_issues(relation, ["relation_protocol"]) == []
    assert profile_patch_contract_issues(mutable, ["mutable_state_lifecycle"]) == []
    assert profile_patch_contract_issues(formatting, ["noninvertible_format_protocol"]) == []


def test_noninvertible_contract_rejects_removing_existing_precision_helper():
    patch = """\
-g_sig_digits = cbook._g_sig_digits(data, delta)
+if isinstance(self.norm, mpl.colors.BoundaryNorm):
+    delta = np.diff(self.norm.boundaries).max()
"""

    issues = profile_patch_contract_issues(patch, ["noninvertible_format_protocol"])

    assert "profile_contract:noninvertible_format_protocol:removed_existing_sig_digits_helper" in issues


def test_profile_context_ranges_find_clean_protocol_definitions(tmp_path):
    path = tmp_path / "lib" / "matplotlib" / "colors.py"
    path.parent.mkdir(parents=True)
    path.write_text("line\nclass BoundaryNorm:\n    pass\n", encoding="utf-8")

    profile = next(
        item for item in PROFILES if item.name == "noninvertible_format_protocol"
    )
    ranges = derive_profile_context_ranges(tmp_path, [profile])

    assert "lib/matplotlib/colors.py" in ranges
    assert ranges["lib/matplotlib/colors.py"][0][0] == 2


def test_mutable_profile_edit_ranges_include_coupled_update_methods(tmp_path):
    path = tmp_path / "lib" / "matplotlib" / "widgets.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "def _update_val_from_pos(self, pos):\n"
        "    pass\n"
        "def _update(self, event):\n"
        "    pass\n"
        "def set_val(self, val):\n"
        "    pass\n",
        encoding="utf-8",
    )

    ranges = derive_profile_edit_ranges(tmp_path, ["mutable_state_lifecycle"])

    assert "lib/matplotlib/widgets.py" in ranges
    assert len(ranges["lib/matplotlib/widgets.py"]) == 3
