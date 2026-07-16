from repo_agent.core.semantic_evidence import (
    blocking_patch_semantic_issues,
    build_verification_plan,
    extract_json_object,
    implementation_semantic_issues,
    normalize_semantic_evidence,
    patch_evidence_alignment_issues,
    patch_semantic_warnings,
    semantic_evidence_warnings,
    validate_semantic_evidence,
    verification_coverage_issues,
)


def _ref(location="sklearn/utils/multiclass.py:155"):
    return [{"type": "code", "location": location, "claim": "observed current behavior"}]


def _pandas_evidence():
    return {
        "reproduction_contract": {
            "trigger": ["nullable pandas labels"],
            "expected_behavior": ["same result as native dtype"],
        },
        "target_partitions": [
            {
                "id": "nullable_series_1d",
                "container": "pandas Series",
                "rank": 1,
                "dtype_protocol": ".dtype",
                "missing_value_behavior": "pd.NA is accepted",
                "first_dependent_predicate": {
                    "symbol": "is_multilabel",
                    "location": "sklearn/utils/multiclass.py:155",
                },
                "expected": "binary labels",
                "evidence": [{"type": "issue", "location": "problem_statement"}],
            },
            {
                "id": "nullable_dataframe_2d",
                "container": "pandas DataFrame",
                "rank": 2,
                "dtype_protocol": ".dtypes",
                "missing_value_behavior": "pd.NA is accepted",
                "first_dependent_predicate": {
                    "symbol": "is_multilabel",
                    "location": "sklearn/utils/multiclass.py:155",
                },
                "expected": "multioutput classification",
                "evidence": _ref(),
            },
        ],
        "differential_cases": [
            {
                "id": "nullable_series_1d",
                "expected": "binary labels",
                "evidence": _ref("sklearn/utils/multiclass.py:155"),
            },
            {
                "id": "nullable_dataframe_2d",
                "expected": "multioutput classification",
                "evidence": _ref("sklearn/utils/multiclass.py:155"),
            },
            {"id": "native_dtype", "expected": "unchanged", "evidence": _ref("sklearn/utils/multiclass.py:343")},
        ],
        "verify_commands": [
            {
                "command": "python -c \"assert True\"",
                "covers": ["nullable_series_1d", "nullable_dataframe_2d", "native_dtype"],
            }
        ],
    }


def test_nullable_semantic_evidence_requires_series_dataframe_and_missing_behavior():
    problem = "Support nullable pandas dtypes in unique_labels"
    evidence = _pandas_evidence()

    assert validate_semantic_evidence(evidence, problem) == []

    evidence["target_partitions"] = evidence["target_partitions"][:1]
    issues = validate_semantic_evidence(evidence, problem)
    assert any("DataFrame" in issue for issue in issues)
    assert implementation_semantic_issues(issues) == []
    assert any("DataFrame" in warning for warning in semantic_evidence_warnings(issues))


def test_star_argument_contract_requires_current_producer_and_two_consumers():
    evidence = {
        "reproduction_contract": {"trigger": ["documented star args"], "expected_behavior": ["no duplicates"]},
        "target_partitions": [
            {
                "id": "class_and_init_render",
                "expected": "one complete rendered entry",
                "evidence": [{"type": "issue", "location": "problem_statement"}],
            }
        ],
        "contract_edges": [
            {
                "producer": "record_typehints at sphinx/ext/autodoc/typehints.py:15",
                "current_canonical_identity": "plain param.name",
                "preferred_change_layer": "consumer",
                "consumers": [
                    {
                        "symbol": "modify_field_list",
                        "location": "sphinx/ext/autodoc/typehints.py:89",
                        "lookup_identity": "plain annotation key matched against plain/starred field aliases",
                        "output_identity": "the actually matched RST field name",
                    },
                    {
                        "symbol": "augment_descriptions_with_types",
                        "location": "sphinx/ext/autodoc/typehints.py:141",
                        "lookup_identity": "plain annotation key matched against description aliases",
                        "output_identity": "the actually matched description field name",
                    },
                ],
                "evidence": _ref("sphinx/ext/autodoc/typehints.py:30"),
            }
        ],
        "verify_commands": [
            {
                "command": "python -m pytest tests/test_ext_autodoc_configs.py -q",
                "covers": ["class_and_init_render"],
            }
        ],
    }

    problem = "Fix duplicated *args and **kwargs with autodoc_typehints"
    assert validate_semantic_evidence(evidence, problem) == []

    edge = evidence["contract_edges"][0]
    consumers = edge["consumers"]
    evidence["contract_edges"] = [
        {**edge, "consumers": [consumers[0]]},
        {**edge, "consumers": [consumers[1]]},
    ]
    assert validate_semantic_evidence(evidence, problem) == []

    evidence["contract_edges"] = evidence["contract_edges"][:1]
    issues = validate_semantic_evidence(evidence, problem)
    assert any("both direct consumers" in issue for issue in issues)
    assert implementation_semantic_issues(issues) == []
    assert semantic_evidence_warnings(issues) == issues


def test_normalize_semantic_evidence_removes_absolute_repo_cd_prefix():
    evidence = _pandas_evidence()
    evidence["verify_commands"][0]["command"] = 'cd /repo && python -c "assert True"'

    normalized = normalize_semantic_evidence(evidence, "Support nullable pandas dtypes in unique_labels")

    assert normalized["verify_commands"][0]["command"] == 'python -c "assert True"'


def test_star_argument_contract_rejects_conflicting_producer_partition():
    evidence = {
        "reproduction_contract": {"trigger": ["documented star args"], "expected_behavior": ["no duplicates"]},
        "target_partitions": [
            {
                "id": "producer_record_typehints",
                "container": "record_typehints",
                "expected": "annotation keys must include the asterisk prefix, not just param.name",
                "evidence": [{"type": "code", "location": "sphinx/ext/autodoc/typehints.py:30"}],
            }
        ],
        "contract_edges": [
            {
                "producer": "record_typehints",
                "current_canonical_identity": "plain param.name",
                "preferred_change_layer": "consumer",
                "consumers": [
                    {
                        "symbol": "modify_field_list",
                        "location": "sphinx/ext/autodoc/typehints.py:89",
                        "lookup_identity": "plain or starred alias",
                        "output_identity": "matched field name",
                    },
                    {
                        "symbol": "augment_descriptions_with_types",
                        "location": "sphinx/ext/autodoc/typehints.py:141",
                        "lookup_identity": "plain or starred alias",
                        "output_identity": "matched field name",
                    },
                ],
                "evidence": [{"type": "code", "location": "sphinx/ext/autodoc/typehints.py:30"}],
            }
        ],
        "differential_cases": [
            {
                "id": "star_args",
                "expected": "one rendered field",
                "evidence": [{"type": "issue", "location": "problem_statement"}],
            }
        ],
        "verify_commands": [{"command": 'python -c "assert True"', "covers": ["star_args"]}],
    }

    issues = validate_semantic_evidence(
        evidence,
        "Fix duplicated *args and **kwargs with autodoc_typehints",
    )

    assert any("conflicts with target_partitions" in issue for issue in issues)
    assert any(
        "conflicts with target_partitions" in issue
        for issue in implementation_semantic_issues(issues)
    )


def test_patch_alignment_detects_broad_object_coercion_and_producer_identity_change():
    pandas_patch = (
        "+    if y.dtype == object:\n"
        "+        try:\n"
        "+            y = xp.asarray(y, dtype=np.float64)\n"
        "+        except (ValueError, TypeError):\n"
        "+            pass\n"
    )
    pandas_issues = patch_evidence_alignment_issues(pandas_patch, _pandas_evidence())
    assert any("all_numeric_object_arrays" in issue for issue in pandas_issues)
    assert any("missing_values" in issue for issue in pandas_issues)
    assert blocking_patch_semantic_issues(pandas_issues) == []
    assert patch_semantic_warnings(pandas_issues) == pandas_issues

    sphinx_evidence = {
        "contract_edges": [
            {
                "producer": "record_typehints",
                "current_canonical_identity": "plain param.name",
                "preferred_change_layer": "consumer",
            }
        ]
    }
    sphinx_patch = (
        "diff --git a/sphinx/ext/autodoc/typehints.py b/sphinx/ext/autodoc/typehints.py\n"
        "@@ -27,3 +27,5 @@ def record_typehints(app, objtype, name, obj, options, args):\n"
        "+    if param.kind == param.VAR_POSITIONAL:\n"
        "+        name = '*' + name\n"
    )
    sphinx_issues = patch_evidence_alignment_issues(sphinx_patch, sphinx_evidence)
    assert any("canonical_identity_changed" in issue for issue in sphinx_issues)
    assert any("consumer_compatibility_required" in issue for issue in sphinx_issues)
    assert blocking_patch_semantic_issues(sphinx_issues) == sphinx_issues
    assert patch_semantic_warnings(sphinx_issues) == []


def test_verification_coverage_requires_all_semantic_cases():
    evidence = _pandas_evidence()
    result = {"status": "passed", "covered_cases": ["nullable_series_1d"]}

    issues = verification_coverage_issues(result, evidence)

    assert issues
    assert "nullable_dataframe_2d" in issues[0]


def test_verification_plan_uses_one_command_covering_every_case():
    evidence = _pandas_evidence()

    plan = build_verification_plan(evidence)

    assert plan == {
        "command": 'python -c "assert True"',
        "covers": ["native_dtype", "nullable_dataframe_2d", "nullable_series_1d"],
    }


def test_single_behavior_command_normalizes_declared_cover_aliases():
    evidence = _pandas_evidence()
    evidence["verify_commands"] = [
        {
            "command": 'python -c "assert True"',
            "covers": ["model-chosen-series-name", "model-chosen-dataframe-name"],
        }
    ]

    assert validate_semantic_evidence(evidence, "Support nullable pandas dtypes in unique_labels") == []
    assert build_verification_plan(evidence) == {
        "command": 'python -c "assert True"',
        "covers": ["native_dtype", "nullable_dataframe_2d", "nullable_series_1d"],
    }


def test_semantic_evidence_rejects_coverage_split_across_commands():
    evidence = _pandas_evidence()
    evidence["verify_commands"] = [
        {"command": 'python -c "assert True"', "covers": ["nullable_series_1d"]},
        {
            "command": 'python -c "assert True"',
            "covers": ["nullable_dataframe_2d", "native_dtype"],
        },
    ]

    issues = validate_semantic_evidence(evidence, "Support nullable pandas dtypes in unique_labels")

    assert "one verify command must directly cover every required semantic case" in issues


def test_extract_json_object_accepts_unescaped_newline_inside_command_string():
    text = '```json\n{"verify_commands":[{"command":"python -c \\"print(1)\nprint(2)\\""}]}\n```'

    evidence = extract_json_object(text)

    assert evidence is not None
    assert "print(2)" in evidence["verify_commands"][0]["command"]


def test_nullable_representation_uses_rank_and_rejects_ambiguous_expected_behavior():
    evidence = _pandas_evidence()
    evidence["target_partitions"][0]["container"] = "type_of_target result"
    evidence["target_partitions"][0]["rank"] = "1d pandas Series"
    evidence["target_partitions"][1]["expected"] = "multilabel-indicator or appropriate error"

    issues = validate_semantic_evidence(evidence, "Support nullable pandas dtypes")

    assert not any("Series representation" in issue for issue in issues)
    assert any("one exact expected public classification" in issue for issue in issues)


def test_patch_alignment_rejects_normalization_after_first_dependent_predicate():
    evidence = _pandas_evidence()
    patch = (
        "diff --git a/sklearn/utils/multiclass.py b/sklearn/utils/multiclass.py\n"
        "@@ -312,3 +312,7 @@ def type_of_target(y, input_name=''):\n"
        "+    if y.dtype == object:\n"
        "+        y = xp.asarray(y, dtype=float)\n"
    )

    issues = patch_evidence_alignment_issues(patch, evidence)

    assert any("normalization_after_first_dependent_predicate" in issue for issue in issues)


def test_patch_alignment_rejects_consumer_alias_not_propagated_to_output():
    evidence = {
        "contract_edges": [
            {
                "producer": "record_typehints",
                "current_canonical_identity": "plain param.name",
                "preferred_change_layer": "consumer",
            }
        ]
    }
    patch = (
        "diff --git a/sphinx/ext/autodoc/typehints.py b/sphinx/ext/autodoc/typehints.py\n"
        "@@ -114,6 +114,8 @@ def modify_field_list(node, annotations):\n"
        "     arg = arguments.get(name, {})\n"
        "+    if not arg:\n"
        "+        arg = arguments.get('*' + name, {})\n"
        "     field += nodes.field_name('', 'type ' + name)\n"
    )

    issues = patch_evidence_alignment_issues(patch, evidence)

    assert any("matched_alias_not_propagated_to_output" in issue for issue in issues)


def test_patch_alignment_rejects_star_alias_patch_that_rewrites_return_type_branches():
    evidence = {
        "contract_edges": [
            {
                "producer": "record_typehints",
                "current_canonical_identity": "plain param.name",
                "preferred_change_layer": "consumer",
            }
        ]
    }
    patch = (
        "diff --git a/sphinx/ext/autodoc/typehints.py b/sphinx/ext/autodoc/typehints.py\n"
        "@@ -156,12 +156,8 @@ def augment_descriptions_with_types(node, annotations):\n"
        "-        elif parts[0] == 'type':\n"
        "-            has_type.add(name)\n"
        "-        elif parts[0] in ('return', 'returns'):\n"
        "-            has_description.add('return')\n"
        "-        elif parts[0] == 'rtype':\n"
        "+    for name in annotations:\n"
        "+        display_name = '*' + name\n"
        "+        has_type.add('return')\n"
    )

    issues = patch_evidence_alignment_issues(patch, evidence)

    assert "semantic_contract_violation:parameter_alias_patch_changed_return_rtype_branches" in issues


def test_patch_alignment_allows_local_star_alias_mapping_without_return_changes():
    evidence = {
        "contract_edges": [
            {
                "producer": "record_typehints",
                "current_canonical_identity": "plain param.name",
                "preferred_change_layer": "consumer",
            }
        ]
    }
    patch = (
        "diff --git a/sphinx/ext/autodoc/typehints.py b/sphinx/ext/autodoc/typehints.py\n"
        "@@ -115,3 +115,7 @@ def modify_field_list(node, annotations):\n"
        "+        matched_name = name\n"
        "+        if '*' + name in arguments:\n"
        "+            matched_name = '*' + name\n"
        "+        field += nodes.field_name('', 'type ' + matched_name)\n"
    )

    issues = patch_evidence_alignment_issues(patch, evidence)

    assert not any("return_rtype_branches" in issue for issue in issues)


def test_semantic_evidence_rejects_internal_simulation_as_behavior_verification():
    evidence = _pandas_evidence()
    evidence["verify_commands"] = [
        {
            "command": 'python -c "assert True; print(\'BUG CONFIRMED: would append field\')"',
            "covers": ["nullable_series_1d", "nullable_dataframe_2d", "native_dtype"],
        }
    ]

    issues = validate_semantic_evidence(evidence, "Support nullable pandas dtypes")

    assert any("simulates internal data flow" in issue for issue in issues)


def test_semantic_evidence_rejects_verifier_that_asserts_nullable_bug_persists():
    evidence = _pandas_evidence()
    evidence["verify_commands"] = [
        {
            "command": (
                "python -c \"from sklearn.utils.multiclass import type_of_target; "
                "assert type_of_target(y) == 'unknown', 'expected unknown (buggy behavior)'\""
            ),
            "covers": ["nullable_series_1d"],
        }
    ]

    issues = validate_semantic_evidence(evidence, "Support nullable pandas dtypes in unique_labels")

    assert any("pre-fix" in issue for issue in issues)


def test_star_argument_evidence_normalizes_plain_canonical_storage_to_consumer():
    evidence = {
        "contract_edges": [
            {
                "producer": "record_typehints",
                "current_canonical_identity": "bare param.name (no asterisk)",
                "preferred_change_layer": "producer",
            }
        ]
    }

    normalized = normalize_semantic_evidence(
        evidence,
        "Fix duplicated *args and **kwargs with autodoc_typehints",
    )

    assert normalized is not evidence
    assert normalized["contract_edges"][0]["preferred_change_layer"] == "consumer"
    assert evidence["contract_edges"][0]["preferred_change_layer"] == "producer"


def test_patch_alignment_rejects_first_element_as_nullable_container_dtype_proxy():
    patch = (
        "diff --git a/sklearn/utils/multiclass.py b/sklearn/utils/multiclass.py\n"
        "@@ -342,3 +342,6 @@ def type_of_target(y, input_name=''):\n"
        "+    if isinstance(y.flat[0], (int, float, bool)):\n"
        "+        return 'binary'\n"
    )

    issues = patch_evidence_alignment_issues(patch, _pandas_evidence())

    assert "semantic_representation_gap:first_element_used_as_container_dtype_proxy" in issues


def test_star_argument_verifier_rejects_bare_param_presence_assertion():
    evidence = {
        "reproduction_contract": {"trigger": ["documented *args"], "expected_behavior": ["no duplicate fields"]},
        "target_partitions": [
            {
                "id": "star_args",
                "expected": "one starred rendered parameter",
                "evidence": [{"type": "issue", "location": "problem_statement"}],
            }
        ],
        "contract_edges": [
            {
                "producer": "record_typehints",
                "current_canonical_identity": "plain param.name",
                "preferred_change_layer": "consumer",
                "consumers": [
                    {
                        "symbol": "modify_field_list",
                        "location": "sphinx/ext/autodoc/typehints.py:89",
                        "lookup_identity": "args or *args",
                        "output_identity": "param *args",
                    },
                    {
                        "symbol": "augment_descriptions_with_types",
                        "location": "sphinx/ext/autodoc/typehints.py:141",
                        "lookup_identity": "args or *args",
                        "output_identity": "param *args",
                    },
                ],
                "evidence": [{"type": "code", "location": "sphinx/ext/autodoc/typehints.py:30"}],
            }
        ],
        "differential_cases": [
            {
                "id": "star_args",
                "expected": "starred param remains and bare duplicate is absent",
                "evidence": [{"type": "issue", "location": "problem_statement"}],
            }
        ],
        "verify_commands": [
            {
                "command": (
                    "python -c \"names=['param *args']; "
                    "assert len([x for x in names if 'param args' in x]) == 1\""
                ),
                "covers": ["star_args"],
            }
        ],
    }

    issues = validate_semantic_evidence(
        evidence,
        "Fix duplicated *args and **kwargs with autodoc_typehints",
    )

    assert any("collapses lookup and rendered identities" in issue for issue in issues)


def test_star_argument_verifier_rejects_bare_type_output_and_internal_only_check():
    evidence = {
        "reproduction_contract": {"trigger": ["documented *args"], "expected_behavior": ["rendered type"]},
        "target_partitions": [
            {
                "id": "star_args",
                "expected": "starred parameter renders with its annotation type",
                "evidence": [{"type": "issue", "location": "problem_statement"}],
            }
        ],
        "contract_edges": [
            {
                "producer": "record_typehints",
                "current_canonical_identity": "plain param.name",
                "preferred_change_layer": "consumer",
                "consumers": [
                    {
                        "symbol": "modify_field_list",
                        "location": "sphinx/ext/autodoc/typehints.py:89",
                        "lookup_identity": "args",
                        "output_identity": "param *args and type *args",
                    },
                    {
                        "symbol": "augment_descriptions_with_types",
                        "location": "sphinx/ext/autodoc/typehints.py:141",
                        "lookup_identity": "args",
                        "output_identity": "param *args and type *args",
                    },
                ],
                "evidence": [{"type": "code", "location": "sphinx/ext/autodoc/typehints.py:30"}],
            }
        ],
        "differential_cases": [
            {
                "id": "star_args",
                "expected": "rendered *args includes int",
                "evidence": [{"type": "issue", "location": "problem_statement"}],
            }
        ],
        "verify_commands": [
            {
                "command": (
                    "python -c \"from docutils import nodes; "
                    "from sphinx.ext.autodoc.typehints import modify_field_list; "
                    "fields=nodes.field_list(); modify_field_list(fields, {'args':'int'}); "
                    "assert any('type args' in field[0].astext() for field in fields)\""
                ),
                "covers": ["star_args"],
            }
        ],
    }

    issues = validate_semantic_evidence(
        evidence,
        "Fix duplicated *args and **kwargs with autodoc_typehints",
    )

    assert any("drops the matched display identity" in issue for issue in issues)

    evidence["verify_commands"][0]["command"] = (
        "python -c \"from docutils import nodes; "
        "from sphinx.ext.autodoc.typehints import modify_field_list; "
        "fields=nodes.field_list(); modify_field_list(fields, {'args':'int'}); "
        "assert not any('type args' in field[0].astext() for field in fields); "
        "assert any('type *args' in field[0].astext() for field in fields)\""
    )
    issues = validate_semantic_evidence(
        evidence,
        "Fix duplicated *args and **kwargs with autodoc_typehints",
    )

    assert not any("drops the matched display identity" in issue for issue in issues)
    assert any("only checks intermediate field-list helpers" in issue for issue in issues)
    assert build_verification_plan(evidence) is None

    evidence["contract_edges"][0]["current_canonical_identity"] = "args"
    assert build_verification_plan(evidence) is None
