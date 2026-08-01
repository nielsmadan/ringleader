from ringleader.projects import matches, filter_by_projects


def _thread(sid="s1", project="/r/repo/sub", git_label="repo",
            git_key="github.com/me/repo", git_root="/r/repo"):
    return {
        "sessionId": sid, "project": project,
        "gitProjectLabel": git_label, "gitProjectKey": git_key,
        "gitRoot": git_root,
    }


def test_matches_label_key_root_cwd_equality_case_insensitive():
    t = _thread()
    assert matches(t, "repo")
    assert matches(t, "REPO")                       # case-insensitive
    assert matches(t, "github.com/me/repo")         # key
    assert matches(t, "/r/repo")                    # git root (exact)
    assert matches(t, "/r/repo/sub")                # cwd (exact)


def test_matches_path_prefix_on_root_and_cwd():
    t = _thread()
    assert matches(t, "/r")                          # prefix of root + cwd
    assert matches(t, "/r/repo")                     # directory-boundary prefix of cwd


def test_matches_non_match():
    t = _thread()
    assert not matches(t, "other-repo")
    assert not matches(t, "/different/path")


def test_matches_path_prefix_respects_directory_boundary():
    # selector /r/repo must NOT match a sibling directory /r/repo2
    sibling = {"sessionId": "s2", "project": "/r/repo2",
                "gitProjectLabel": None, "gitProjectKey": None, "gitRoot": "/r/repo2"}
    assert not matches(sibling, "/r/repo")

    # but a true sub-path /r/repo/sub MUST still match
    sub = {"sessionId": "s3", "project": "/r/repo/sub",
           "gitProjectLabel": None, "gitProjectKey": None, "gitRoot": "/r/repo"}
    assert matches(sub, "/r/repo")


def test_matches_tolerates_missing_git_fields():
    t = {"sessionId": "x", "project": "/only/cwd",
         "gitProjectLabel": None, "gitProjectKey": None, "gitRoot": None}
    assert matches(t, "/only/cwd")
    assert matches(t, "/only")
    assert not matches(t, "repo")


def test_filter_by_projects_any_match_and_empty_is_all():
    a = _thread("a", git_label="repo-a", git_key="k-a", git_root="/r/a", project="/r/a")
    b = _thread("b", git_label="repo-b", git_key="k-b", git_root="/r/b", project="/r/b")
    assert filter_by_projects([a, b], None) == [a, b]      # None → all
    assert filter_by_projects([a, b], []) == [a, b]        # empty → all
    assert filter_by_projects([a, b], ["repo-a"]) == [a]
    assert filter_by_projects([a, b], ["repo-a", "repo-b"]) == [a, b]


from ringleader.projects import project_summary, ProjectStat


def _t(sid, status="finished", title=None, git_key="k", git_label="repo", project="/p"):
    return {
        "sessionId": sid, "status": status, "title": title,
        "project": project, "gitProjectKey": git_key, "gitProjectLabel": git_label,
        "gitRoot": None,
    }


def _reg(threads):
    return {"version": 1, "lastScan": None, "threads": threads, "tombstones": []}


def test_project_summary_counts_total_classified_unnamed():
    reg = _reg([
        _t("a", status="finished", title=None,    git_key="k1", git_label="repo1"),
        _t("b", status="blocked",  title="named",  git_key="k1", git_label="repo1"),
        _t("c", status="unclassified", title=None, git_key="k1", git_label="repo1"),
    ])
    stats = project_summary(reg)
    assert len(stats) == 1
    s = stats[0]
    assert isinstance(s, ProjectStat)
    assert s.label == "repo1"
    assert s.total == 3
    assert s.classified == 2          # finished + blocked
    assert s.unnamed == 1             # classified AND no title → only "a"


def test_project_summary_orders_git_first_then_by_total_desc():
    reg = _reg([
        _t("a", git_key="k-big", git_label="big"),
        _t("b", git_key="k-big", git_label="big"),
        _t("c", git_key="k-small", git_label="small"),
        # non-git cwd group (no gitProjectKey)
        _t("d", git_key=None, git_label=None, project="/lonely"),
    ])
    stats = project_summary(reg)
    labels = [s.label for s in stats]
    assert labels[0] == "big"          # git, total 2
    assert labels[1] == "small"        # git, total 1
    assert labels[-1] == "/lonely"     # non-git bucket last
    assert stats[0].is_git is True
    assert stats[-1].is_git is False


def test_project_summary_lists_distinct_checkouts_sorted():
    # one repo (same git key) touched from three different checkouts, one repeated
    reg = _reg([
        _t("a", git_key="k1", git_label="repo1", project="/co/c2"),
        _t("b", git_key="k1", git_label="repo1", project="/co/c1"),
        _t("c", git_key="k1", git_label="repo1", project="/co/c1"),
        _t("d", git_key="k1", git_label="repo1", project="/co/c3"),
    ])
    s = project_summary(reg)[0]
    assert s.checkouts == ["/co/c1", "/co/c2", "/co/c3"]   # distinct, sorted
    assert len(s.checkouts) == 3


def _mem(name, git_key="k1"):
    return {"path": f"/p/-enc/memory/{name}.md", "project": "/p", "name": name,
            "gitProjectKey": git_key, "gitProjectLabel": "repo1",
            "obsoleteSignals": []}


def test_project_summary_counts_memory_entries_and_clean_stamp():
    reg = _reg([_t("a", git_key="k1", git_label="repo1")])
    reg["memoryEntries"] = [_mem("m1"), _mem("m2")]
    reg["memoryProjectsCleanedAt"] = {"k1": "2026-06-20T00:00:00+00:00"}
    s = project_summary(reg)[0]
    assert s.mem_entries == 2
    assert s.mem_cleaned_at == "2026-06-20T00:00:00+00:00"


def test_project_summary_memory_defaults_when_registry_has_no_memory_keys():
    s = project_summary(_reg([_t("a", git_key="k1", git_label="repo1")]))[0]
    assert s.mem_entries == 0 and s.mem_cleaned_at is None


def test_project_summary_memory_stamp_does_not_affect_session_attention():
    # a memory clean must not make an untriaged project look calm
    reg = _reg([_t("a", git_key="k1", git_label="repo1")])
    reg["memoryProjectsCleanedAt"] = {"k1": "2026-06-20T00:00:00+00:00"}
    s = project_summary(reg)[0]
    assert s.needs_attention is True


def test_project_summary_never_cleaned_needs_attention():
    s = project_summary(_reg([_t("a", git_key="k1", git_label="repo1")]))[0]
    assert s.cleaned_at is None and s.needs_attention is True


def test_project_summary_cleaned_with_no_new_activity_is_calm():
    reg = _reg([dict(_t("a", git_key="k1", git_label="repo1"),
                     mtime="2026-06-19T00:00:00+00:00")])
    reg["projectsCleanedAt"] = {"k1": "2026-06-20T00:00:00+00:00"}
    s = project_summary(reg)[0]
    assert s.cleaned_at == "2026-06-20T00:00:00+00:00"
    assert s.needs_attention is False


def test_project_summary_flags_new_activity_since_clean():
    reg = _reg([dict(_t("a", git_key="k1", git_label="repo1"),
                     mtime="2026-06-25T00:00:00+00:00")])
    reg["projectsCleanedAt"] = {"k1": "2026-06-20T00:00:00+00:00"}
    assert project_summary(reg)[0].needs_attention is True


def test_project_summary_orders_attention_first():
    reg = _reg([
        dict(_t("a", git_key="k-clean", git_label="cleaned"),
             mtime="2026-06-19T00:00:00+00:00"),
        _t("b", git_key="k-dirty", git_label="dirty"),     # never cleaned
    ])
    reg["projectsCleanedAt"] = {"k-clean": "2026-06-20T00:00:00+00:00"}
    labels = [s.label for s in project_summary(reg)]
    assert labels[0] == "dirty" and labels[1] == "cleaned"


def test_project_summary_hides_snoozed_by_default():
    reg = _reg([
        _t("a", git_key="k1", git_label="repo1"),
        dict(_t("b", git_key="k1", git_label="repo1"), snoozedAt="2026-07-01T00:00:00+00:00"),
    ])
    s = project_summary(reg)[0]
    assert s.total == 1 and s.snoozed == 0            # snoozed 'b' excluded


def test_project_summary_all_snoozed_project_disappears():
    reg = _reg([dict(_t("a", git_key="k1", git_label="repo1"),
                     snoozedAt="2026-07-01T00:00:00+00:00")])
    assert project_summary(reg) == []                  # nothing visible
    stats = project_summary(reg, include_snoozed=True)
    assert len(stats) == 1 and stats[0].total == 1 and stats[0].snoozed == 1


def test_project_summary_snoozed_does_not_flag_attention():
    reg = _reg([dict(_t("a", git_key="k1", git_label="repo1"),
                     mtime="2026-06-25T00:00:00+00:00",
                     snoozedAt="2026-07-01T00:00:00+00:00")])
    reg["projectsCleanedAt"] = {"k1": "2026-06-20T00:00:00+00:00"}
    stats = project_summary(reg, include_snoozed=True)
    # 'a' changed after clean, but it's snoozed → must not raise the ⚠ flag
    assert stats[0].needs_attention is False
