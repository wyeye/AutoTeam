from autoteam import sync_targets


def test_get_sync_target_states_uses_implicit_config_presence():
    env = {
        "CPA_URL": "http://127.0.0.1:8317",
        "CPA_KEY": "key-1",
        "SUB2API_URL": "http://sub2api.local",
        "SUB2API_EMAIL": "admin@example.com",
        "SUB2API_PASSWORD": "secret",
    }

    assert sync_targets.get_sync_target_states(env) == {
        "cpa": True,
        "sub2api": True,
    }


def test_get_sync_target_states_respects_explicit_toggle_override():
    env = {
        "SYNC_TARGET_CPA": "false",
        "CPA_URL": "http://127.0.0.1:8317",
        "CPA_KEY": "key-1",
        "SYNC_TARGET_SUB2API": "true",
    }

    assert sync_targets.get_sync_target_states(env) == {
        "cpa": False,
        "sub2api": True,
    }


def test_describe_sync_targets_formats_labels():
    assert sync_targets.describe_sync_targets(["cpa"]) == "CPA"
    assert sync_targets.describe_sync_targets(["cpa", "sub2api"]) == "CPA + Sub2API"


def test_delete_account_from_configured_targets_records_each_target_error(monkeypatch):
    monkeypatch.setattr(sync_targets, "get_available_sync_targets", lambda: ["cpa", "sub2api"])

    import autoteam.cpa_sync as cpa_sync
    import autoteam.sub2api_sync as sub2api_sync

    monkeypatch.setattr(cpa_sync, "list_cpa_files", lambda: (_ for _ in ()).throw(ConnectionError("cpa down")))
    monkeypatch.setattr(
        sub2api_sync,
        "delete_account_from_sub2api",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("sub2api down")),
    )

    result = sync_targets.delete_account_from_configured_targets(
        "user@example.com",
        include_disabled=True,
    )

    assert result["cpa"]["deleted"] == []
    assert result["cpa"]["count"] == 0
    assert result["cpa"]["error"] == "cpa down"
    assert result["sub2api"]["deleted"] == []
    assert result["sub2api"]["count"] == 0
    assert result["sub2api"]["error"] == "sub2api down"
