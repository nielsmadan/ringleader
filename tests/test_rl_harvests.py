import json
import subprocess


def _write_items(tmp_path, monkeypatch, bodies):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    from ringleader import harvests

    paths = [
        harvests.write_harvest(
            session_id=f"session-{index}",
            name=f"harvest-{index}",
            folder_path="/repo",
            repo_url="github.com/me/repo",
            harvested_at=f"2026-08-29T00:00:0{index}+00:00",
            content=body,
        )
        for index, body in enumerate(bodies)
    ]
    by_path = {item.path: item for item in harvests.snapshot_harvests().items}
    return [by_path[path] for path in paths]


def _clean_checkout(tmp_path, name="checkout"):
    checkout = tmp_path / name
    checkout.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
        ("commit", "--allow-empty", "-qm", "initial"),
    ):
        subprocess.run(["git", "-C", str(checkout), *args], check=True)
    head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    branch = subprocess.run(
        ["git", "-C", str(checkout), "symbolic-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return checkout, branch, head


def _begin_args(item, checkout, branch, base, registry, bundle):
    return [
        "begin",
        "--repo-key",
        item.repo_key,
        "--checkout",
        str(checkout),
        "--branch",
        branch,
        "--base",
        base,
        "--registry",
        str(registry),
        "--bundle",
        str(bundle),
        "--item",
        item.token,
    ]


def _begin_receipt(item, checkout, branch, base, registry, bundle):
    from ringleader.publish_state import begin_publish

    return begin_publish(
        repo_key=item.repo_key,
        checkout=str(checkout),
        branch=branch,
        base_commit=base,
        registry_path=str(registry),
        tokens=[item.token],
        bundle_path=bundle,
    )


def test_snapshot_json_omits_bodies_and_includes_exact_metadata(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    from ringleader import harvests
    from ringleader.commands import harvests as cmd

    path = harvests.write_harvest(
        session_id="s1",
        name="n",
        folder_path="/r",
        repo_url="u",
        harvested_at="t",
        content="secret body",
    )
    item = next(item for item in harvests.snapshot_harvests().items if item.path == path)

    assert cmd.cmd_harvests(["snapshot", "--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["items"][0]["token"] == item.token
    assert data["items"][0]["contentBytes"] == len(b"secret body")
    assert "content" not in data["items"][0]


def test_cli_begin_claims_repeated_items_writes_private_bundle_and_emits_one_json(
    tmp_path, monkeypatch, capsys
):
    items = _write_items(tmp_path, monkeypatch, ["first", "second"])
    from ringleader.commands import harvests as cmd

    checkout, branch, base = _clean_checkout(tmp_path)
    bundle = tmp_path / "private" / "inputs.md"
    args = _begin_args(items[0], checkout, branch, base, tmp_path / "registry.json", bundle)
    args.extend(("--item", items[1].token))

    assert cmd.cmd_harvests(args) == 0

    output = capsys.readouterr()
    data = json.loads(output.out)
    assert output.out.strip().startswith("{") and output.out.strip().endswith("}")
    assert output.err == ""
    assert [row["token"] for row in data["receipt"]["items"]] == [item.token for item in items]
    assert bundle.stat().st_mode & 0o777 == 0o600
    assert "first" in bundle.read_text() and "second" in bundle.read_text()


def test_cli_prepare_records_changed_and_no_change_outcomes_and_enforces_their_arguments(
    tmp_path, monkeypatch, capsys
):
    items = _write_items(tmp_path, monkeypatch, ["changed", "unchanged"])
    from ringleader.commands import harvests as cmd

    checkout, branch, base = _clean_checkout(tmp_path, "changed-checkout")
    changed = _begin_receipt(
        items[0],
        checkout,
        branch,
        base,
        tmp_path / "changed-registry.json",
        tmp_path / "changed.md",
    )
    for invalid_args in (
        ["--tree", "a" * 40],
        ["--paths-file", str(tmp_path / "missing-paths.txt")],
    ):
        assert (
            cmd.cmd_harvests(["prepare", changed.receipt_id, "--outcome", "changed", *invalid_args])
            == 1
        )
        missing = capsys.readouterr()
        assert missing.out == "" and missing.err.startswith("rl-harvests: changed outcome requires")
    (checkout / "README.md").write_text("# Updated\n")
    subprocess.run(["git", "-C", str(checkout), "add", "README.md"], check=True)
    tree = subprocess.run(
        ["git", "-C", str(checkout), "write-tree"], check=True, capture_output=True, text=True
    ).stdout.strip()
    report = tmp_path / "changed-paths.txt"
    report.write_text("README.md\n")

    assert (
        cmd.cmd_harvests(
            [
                "prepare",
                changed.receipt_id,
                "--outcome",
                "changed",
                "--tree",
                tree,
                "--paths-file",
                str(report),
            ]
        )
        == 0
    )
    changed_output = json.loads(capsys.readouterr().out)
    assert changed_output["receipt"]["outcome"] == "changed"
    assert changed_output["receipt"]["expectedTree"] == tree

    unchanged_checkout, unchanged_branch, unchanged_base = _clean_checkout(
        tmp_path, "unchanged-checkout"
    )
    unchanged = _begin_receipt(
        items[1],
        unchanged_checkout,
        unchanged_branch,
        unchanged_base,
        tmp_path / "unchanged-registry.json",
        tmp_path / "unchanged.md",
    )
    for invalid_args in (
        ["--tree", "a" * 40],
        ["--paths-file", str(tmp_path / "unused-paths.txt")],
    ):
        assert (
            cmd.cmd_harvests(
                ["prepare", unchanged.receipt_id, "--outcome", "no-change", *invalid_args]
            )
            == 1
        )
        forbidden = capsys.readouterr()
        assert forbidden.out == "" and forbidden.err.startswith(
            "rl-harvests: no-change outcome does not accept"
        )

    assert cmd.cmd_harvests(["prepare", unchanged.receipt_id, "--outcome", "no-change"]) == 0
    unchanged_output = json.loads(capsys.readouterr().out)
    assert unchanged_output["receipt"]["outcome"] == "no-change"
    assert unchanged_output["receipt"]["expectedTree"] is None


def test_cli_consume_enforces_commit_arguments_and_consumes_a_no_change_receipt(
    tmp_path, monkeypatch, capsys
):
    items = _write_items(tmp_path, monkeypatch, ["changed", "unchanged"])
    from ringleader.commands import harvests as cmd
    from ringleader.publish_state import prepare_publish

    changed_checkout, changed_branch, changed_base = _clean_checkout(tmp_path, "changed-checkout")
    changed = _begin_receipt(
        items[0],
        changed_checkout,
        changed_branch,
        changed_base,
        tmp_path / "changed-registry.json",
        tmp_path / "changed.md",
    )
    prepare_publish(
        changed.receipt_id,
        outcome="changed",
        expected_tree="b" * 40,
        changed_paths=("README.md",),
    )
    assert cmd.cmd_harvests(["consume", changed.receipt_id]) == 1
    changed_error = capsys.readouterr()
    assert changed_error.out == "" and changed_error.err.startswith(
        "rl-harvests: changed outcome requires a commit"
    )

    unchanged_checkout, unchanged_branch, unchanged_base = _clean_checkout(
        tmp_path, "unchanged-checkout"
    )
    unchanged = _begin_receipt(
        items[1],
        unchanged_checkout,
        unchanged_branch,
        unchanged_base,
        tmp_path / "unchanged-registry.json",
        tmp_path / "unchanged.md",
    )
    prepare_publish(unchanged.receipt_id, outcome="no-change")
    assert cmd.cmd_harvests(["consume", unchanged.receipt_id, "--commit", "c" * 40]) == 1
    forbidden = capsys.readouterr()
    assert forbidden.out == "" and forbidden.err.startswith(
        "rl-harvests: no-change outcome does not accept a commit"
    )

    assert cmd.cmd_harvests(["consume", unchanged.receipt_id]) == 0
    consumed = json.loads(capsys.readouterr().out)
    assert consumed["receiptId"] == unchanged.receipt_id
    assert consumed["consumed"] == 1
    assert not items[1].path.exists()


def test_cli_abandon_removes_a_begun_receipt_and_rejects_a_prepared_one(
    tmp_path, monkeypatch, capsys
):
    items = _write_items(tmp_path, monkeypatch, ["begun", "prepared"])
    from ringleader.commands import harvests as cmd
    from ringleader.publish_state import list_receipts, prepare_publish

    begun_checkout, begun_branch, begun_base = _clean_checkout(tmp_path, "begun-checkout")
    begun = _begin_receipt(
        items[0],
        begun_checkout,
        begun_branch,
        begun_base,
        tmp_path / "begun-registry.json",
        tmp_path / "begun.md",
    )
    assert cmd.cmd_harvests(["abandon", begun.receipt_id]) == 0
    assert json.loads(capsys.readouterr().out) == {"receiptId": begun.receipt_id, "abandoned": True}
    assert begun.receipt_id not in {receipt.receipt_id for receipt in list_receipts()[0]}

    prepared_checkout, prepared_branch, prepared_base = _clean_checkout(
        tmp_path, "prepared-checkout"
    )
    prepared = _begin_receipt(
        items[1],
        prepared_checkout,
        prepared_branch,
        prepared_base,
        tmp_path / "prepared-registry.json",
        tmp_path / "prepared.md",
    )
    prepare_publish(prepared.receipt_id, outcome="no-change")
    assert cmd.cmd_harvests(["abandon", prepared.receipt_id]) == 1
    rejected = capsys.readouterr()
    assert rejected.out == "" and rejected.err.startswith(
        "rl-harvests: receipt cannot be safely abandoned"
    )


def test_cli_reports_publish_git_errors_on_stderr_without_json_output(
    tmp_path, monkeypatch, capsys
):
    item = _write_items(tmp_path, monkeypatch, ["body"])[0]
    from ringleader.commands import harvests as cmd

    args = _begin_args(
        item,
        tmp_path / "not-a-checkout",
        "refs/heads/main",
        "a" * 40,
        tmp_path / "registry.json",
        tmp_path / "bundle.md",
    )

    assert cmd.cmd_harvests(args) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("rl-harvests: checkout is not a Git repository")


def test_recover_json_lists_an_unresolved_receipt(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    from ringleader import harvests
    from ringleader.commands import harvests as cmd
    from ringleader.publish_state import begin_publish

    path = harvests.write_harvest(
        session_id="s1",
        name="n",
        folder_path="/r",
        repo_url="u",
        harvested_at="t",
        content="body",
    )
    item = next(item for item in harvests.snapshot_harvests().items if item.path == path)
    receipt = begin_publish(
        repo_key=item.repo_key,
        checkout=str(tmp_path / "checkout"),
        branch="refs/heads/main",
        base_commit="a" * 40,
        registry_path=str(tmp_path / "registry.json"),
        tokens=[item.token],
        bundle_path=tmp_path / "bundle.md",
    )

    assert cmd.cmd_harvests(["recover", "--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["receipts"][0]["receiptId"] == receipt.receipt_id
    assert data["receipts"][0]["state"] == "begun"


def test_harvests_lists_store(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    from ringleader import harvests
    from ringleader.commands import harvests as cmd

    harvests.write_harvest(
        session_id="s1",
        name="fix-auth",
        folder_path="/repo",
        repo_url="git@github.com:me/repo.git",
        harvested_at="2026-01-01T00:00:00+00:00",
        content="## Summary\n\nx\n",
    )
    rc = cmd.cmd_harvests([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "fix-auth" in out and "git@github.com:me/repo.git" in out and "/repo" in out


def test_harvests_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    import json

    from ringleader import harvests
    from ringleader.commands import harvests as cmd

    harvests.write_harvest(
        session_id="s1",
        name="n",
        folder_path="/r",
        repo_url="u",
        harvested_at="t",
        content="body text",
    )
    rc = cmd.cmd_harvests(["--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["repoUrl"] == "u" and "body text" in data[0]["content"]


def test_harvests_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    from ringleader.commands import harvests as cmd

    assert cmd.cmd_harvests([]) == 0


def test_cli_consume_contract_and_prefixed_state_diagnostic(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    from ringleader import harvests
    from ringleader.commands import harvests as cmd
    from ringleader.publish_state import begin_publish, prepare_publish

    path = harvests.write_harvest(
        session_id="s", name="n", folder_path="/r", repo_url="u", harvested_at="t", content="b"
    )
    item = next(item for item in harvests.snapshot_harvests().items if item.path == path)
    receipt = begin_publish(
        repo_key=item.repo_key,
        checkout=str(tmp_path / "checkout"),
        branch="refs/heads/main",
        base_commit="a" * 40,
        registry_path=str(tmp_path / "registry.json"),
        tokens=[item.token],
        bundle_path=tmp_path / "bundle.md",
    )
    prepare_publish(
        receipt.receipt_id, outcome="changed", expected_tree="b" * 40, changed_paths=("README.md",)
    )
    assert cmd.cmd_harvests(["consume", receipt.receipt_id]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith(
        "rl-harvests: changed outcome requires a commit"
    )


def test_cli_recover_reports_malformed_receipt_problem(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    from ringleader.commands import harvests as cmd
    from ringleader.paths import publish_receipts_dir

    bad = publish_receipts_dir() / "bad.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("not json")
    assert cmd.cmd_harvests(["recover", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["receipts"] == [] and data["problems"][0]["path"] == str(bad)
