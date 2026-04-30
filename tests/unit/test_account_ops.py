import pytest

from autoteam import account_ops


class _FakeChatGPT:
    def __init__(self, responses):
        self._responses = responses

    def _api_fetch(self, method, path):
        return self._responses[path]


def test_fetch_team_state_parses_members_and_invites(monkeypatch):
    monkeypatch.setattr(account_ops, "get_chatgpt_account_id", lambda: "acc-1")
    chatgpt = _FakeChatGPT(
        {
            "/backend-api/accounts/acc-1/users": {
                "status": 200,
                "body": '{"items":[{"email":"member@example.com"}]}',
            },
            "/backend-api/accounts/acc-1/invites": {
                "status": 200,
                "body": '{"invites":[{"email":"invite@example.com"}]}',
            },
        }
    )

    members, invites = account_ops.fetch_team_state(chatgpt)

    assert members == [{"email": "member@example.com"}]
    assert invites == [{"email": "invite@example.com"}]


def test_fetch_team_state_raises_readable_error_when_users_response_is_html(monkeypatch):
    monkeypatch.setattr(account_ops, "get_chatgpt_account_id", lambda: "acc-1")
    chatgpt = _FakeChatGPT(
        {
            "/backend-api/accounts/acc-1/users": {
                "status": 200,
                "body": "<!doctype html><html><body>login</body></html>",
            },
            "/backend-api/accounts/acc-1/invites": {
                "status": 200,
                "body": '{"invites":[]}',
            },
        }
    )

    with pytest.raises(RuntimeError, match="Team 成员接口返回了非 JSON 内容"):
        account_ops.fetch_team_state(chatgpt)


def test_fetch_team_state_raises_readable_error_when_users_auth_fails(monkeypatch):
    monkeypatch.setattr(account_ops, "get_chatgpt_account_id", lambda: "acc-1")
    chatgpt = _FakeChatGPT(
        {
            "/backend-api/accounts/acc-1/users": {
                "status": 403,
                "body": '{"detail":"forbidden"}',
            },
            "/backend-api/accounts/acc-1/invites": {
                "status": 200,
                "body": '{"invites":[]}',
            },
        }
    )

    with pytest.raises(RuntimeError, match="请重新完成管理员登录"):
        account_ops.fetch_team_state(chatgpt)


def test_delete_managed_account_uses_generic_mail_provider_fields(tmp_path, monkeypatch):
    auth_dir = tmp_path / "auths"
    auth_dir.mkdir()
    auth_file = auth_dir / "codex-user@example.com-team.json"
    auth_file.write_text("{}", encoding="utf-8")

    accounts = [
        {
            "email": "user@example.com",
            "status": "standby",
            "auth_file": str(auth_file),
            "mail_provider": "cloudflare_temp_email",
            "mail_account_id": 55,
            "cloudmail_account_id": None,
        }
    ]
    deleted = []

    class _FakeMailClient:
        provider_name = "cloudflare_temp_email"

        def delete_account(self, account_id):
            deleted.append(account_id)
            return {"code": 200}

    monkeypatch.setattr(account_ops, "AUTH_DIR", auth_dir)
    monkeypatch.setattr(account_ops, "load_accounts", lambda: list(accounts))
    monkeypatch.setattr(account_ops, "save_accounts", lambda items: accounts.clear() or accounts.extend(items))
    monkeypatch.setattr(account_ops, "delete_account_from_configured_targets", lambda *args, **kwargs: {})
    monkeypatch.setattr(account_ops, "sync_to_cpa", lambda: None)

    cleanup = account_ops.delete_managed_account(
        "user@example.com",
        remove_remote=False,
        mail_client=_FakeMailClient(),
        sync_cpa_after=False,
    )

    assert deleted == [55]
    assert cleanup["local_record"] is True
    assert cleanup["cloudmail_deleted"] is True


def test_delete_managed_account_hard_uses_full_cleanup_flags(monkeypatch):
    calls = []

    def fake_delete_managed_account(email, **kwargs):
        calls.append((email, kwargs))
        return {"local_record": True}

    monkeypatch.setattr(account_ops, "delete_managed_account", fake_delete_managed_account)

    result = account_ops.delete_managed_account_hard("user@example.com", chatgpt_api="chatgpt")

    assert result == {"local_record": True}
    assert calls == [
        (
            "user@example.com",
            {
                "chatgpt_api": "chatgpt",
                "remove_remote": True,
                "remove_cloudmail": True,
                "sync_cpa_after": True,
                "strict_cloudmail": True,
            },
        )
    ]


def test_delete_managed_account_strict_cloudmail_raises_on_mail_cleanup_failure(tmp_path, monkeypatch):
    auth_dir = tmp_path / "auths"
    auth_dir.mkdir()

    accounts = [
        {
            "email": "user@example.com",
            "status": "standby",
            "auth_file": None,
            "mail_provider": "cloudmail",
            "mail_account_id": 55,
        }
    ]

    class _FailingMailClient:
        provider_name = "cloudmail"

        def delete_account(self, _account_id):
            return {"code": 500, "message": "delete failed"}

    monkeypatch.setattr(account_ops, "AUTH_DIR", auth_dir)
    monkeypatch.setattr(account_ops, "load_accounts", lambda: list(accounts))
    monkeypatch.setattr(account_ops, "save_accounts", lambda items: accounts.clear() or accounts.extend(items))
    monkeypatch.setattr(account_ops, "delete_account_from_configured_targets", lambda *args, **kwargs: {})
    monkeypatch.setattr(account_ops, "sync_to_cpa", lambda: None)

    with pytest.raises(RuntimeError, match="删除邮箱提供者账户失败"):
        account_ops.delete_managed_account(
            "user@example.com",
            remove_remote=False,
            mail_client=_FailingMailClient(),
            strict_cloudmail=True,
        )

    assert accounts == [
        {
            "email": "user@example.com",
            "status": "standby",
            "auth_file": None,
            "mail_provider": "cloudmail",
            "mail_account_id": 55,
        }
    ]


def test_delete_managed_account_full_cleanup_removes_team_invite_targets_and_syncs(tmp_path, monkeypatch):
    auth_dir = tmp_path / "auths"
    auth_dir.mkdir()
    auth_file = auth_dir / "codex-user@example.com-team.json"
    auth_file.write_text("{}", encoding="utf-8")

    accounts = [
        {
            "email": "user@example.com",
            "status": "active",
            "auth_file": str(auth_file),
            "mail_provider": "cloudmail",
            "mail_account_id": None,
        }
    ]
    remote_deletes = []
    sync_calls = []

    class _FakeChatGPT:
        def __init__(self):
            self.calls = []

        def _api_fetch(self, method, path):
            self.calls.append((method, path))
            return {"status": 204, "body": ""}

    chatgpt = _FakeChatGPT()
    members = [{"email": "user@example.com", "user_id": "user-1"}]
    invites = [{"email_address": "user@example.com", "id": "invite-1"}]

    monkeypatch.setattr(account_ops, "AUTH_DIR", auth_dir)
    monkeypatch.setattr(account_ops, "get_chatgpt_account_id", lambda: "acc-1")
    monkeypatch.setattr(account_ops, "load_accounts", lambda: list(accounts))
    monkeypatch.setattr(account_ops, "save_accounts", lambda items: accounts.clear() or accounts.extend(items))
    monkeypatch.setattr(
        account_ops,
        "delete_account_from_configured_targets",
        lambda *args, **kwargs: remote_deletes.append((args, kwargs))
        or {"cpa": {"deleted": ["codex-user@example.com-team.json"]}, "sub2api": {"deleted": ["AutoTeam | user"]}},
    )
    monkeypatch.setattr(account_ops, "sync_to_cpa", lambda: sync_calls.append(True))

    cleanup = account_ops.delete_managed_account(
        "user@example.com",
        chatgpt_api=chatgpt,
        remote_state=(members, invites),
    )

    assert ("DELETE", "/backend-api/accounts/acc-1/users/user-1") in chatgpt.calls
    assert ("DELETE", "/backend-api/accounts/acc-1/invites/invite-1") in chatgpt.calls
    assert remote_deletes == [
        (
            ("user@example.com",),
            {"auth_names": ["codex-user@example.com-team.json"], "include_disabled": True},
        )
    ]
    assert sync_calls == [True]
    assert cleanup["team_member_removed"] is True
    assert cleanup["invite_removed"] is True
    assert cleanup["local_record"] is True
    assert cleanup["local_auth_files"] == ["codex-user@example.com-team.json"]
    assert cleanup["cpa_files"] == ["codex-user@example.com-team.json"]
    assert cleanup["sub2api_accounts"] == ["AutoTeam | user"]
    assert accounts == []


def test_delete_managed_account_respects_remove_remote_flag(tmp_path, monkeypatch):
    auth_dir = tmp_path / "auths"
    auth_dir.mkdir()
    auth_file = auth_dir / "codex-user@example.com-team.json"
    auth_file.write_text("{}", encoding="utf-8")

    accounts = [
        {
            "email": "user@example.com",
            "status": "standby",
            "auth_file": str(auth_file),
            "mail_provider": "cloudmail",
            "mail_account_id": None,
        }
    ]
    remote_deletes = []
    sync_calls = []

    monkeypatch.setattr(account_ops, "AUTH_DIR", auth_dir)
    monkeypatch.setattr(account_ops, "load_accounts", lambda: list(accounts))
    monkeypatch.setattr(account_ops, "save_accounts", lambda items: accounts.clear() or accounts.extend(items))
    monkeypatch.setattr(account_ops, "delete_account_from_configured_targets", lambda *args, **kwargs: remote_deletes.append(args))
    monkeypatch.setattr(account_ops, "sync_to_cpa", lambda: sync_calls.append(True))

    cleanup = account_ops.delete_managed_account(
        "user@example.com",
        remove_remote=False,
        remove_cloudmail=False,
        sync_cpa_after=True,
    )

    assert remote_deletes == []
    assert sync_calls == []
    assert cleanup["local_auth_files"] == ["codex-user@example.com-team.json"]
    assert cleanup["cpa_files"] == []
    assert cleanup["sub2api_accounts"] == []


def test_delete_managed_account_deletes_auth_files_case_insensitively(tmp_path, monkeypatch):
    auth_dir = tmp_path / "auths"
    auth_dir.mkdir()
    auth_file = auth_dir / "codex-user@example.com-team.json"
    auth_file.write_text("{}", encoding="utf-8")

    accounts = [
        {
            "email": "user@example.com",
            "status": "standby",
            "auth_file": None,
            "mail_provider": "cloudmail",
            "mail_account_id": None,
        }
    ]

    monkeypatch.setattr(account_ops, "AUTH_DIR", auth_dir)
    monkeypatch.setattr(account_ops, "load_accounts", lambda: list(accounts))
    monkeypatch.setattr(account_ops, "save_accounts", lambda items: accounts.clear() or accounts.extend(items))
    monkeypatch.setattr(account_ops, "delete_account_from_configured_targets", lambda *args, **kwargs: {})
    monkeypatch.setattr(account_ops, "sync_to_cpa", lambda: None)

    cleanup = account_ops.delete_managed_account(
        "User@Example.com",
        remove_remote=False,
        remove_cloudmail=False,
        sync_cpa_after=False,
    )

    assert cleanup["local_auth_files"] == ["codex-user@example.com-team.json"]
    assert not auth_file.exists()
    assert accounts == []


def test_delete_managed_account_skips_unsafe_auth_file_path(tmp_path, monkeypatch):
    auth_dir = tmp_path / "auths"
    auth_dir.mkdir()
    unsafe_file = tmp_path / "outside.json"
    unsafe_file.write_text("{}", encoding="utf-8")

    accounts = [
        {
            "email": "user@example.com",
            "status": "standby",
            "auth_file": str(unsafe_file),
            "mail_provider": "cloudmail",
            "mail_account_id": None,
        }
    ]

    monkeypatch.setattr(account_ops, "AUTH_DIR", auth_dir)
    monkeypatch.setattr(account_ops, "load_accounts", lambda: list(accounts))
    monkeypatch.setattr(account_ops, "save_accounts", lambda items: accounts.clear() or accounts.extend(items))
    monkeypatch.setattr(account_ops, "delete_account_from_configured_targets", lambda *args, **kwargs: {})
    monkeypatch.setattr(account_ops, "sync_to_cpa", lambda: None)

    cleanup = account_ops.delete_managed_account(
        "user@example.com",
        remove_remote=False,
        remove_cloudmail=False,
        sync_cpa_after=False,
    )

    assert cleanup["local_auth_files"] == []
    assert unsafe_file.exists()
    assert accounts == []


def test_delete_managed_account_skips_mismatched_auth_file_path(tmp_path, monkeypatch):
    auth_dir = tmp_path / "auths"
    auth_dir.mkdir()
    other_auth_file = auth_dir / "codex-other@example.com-team.json"
    other_auth_file.write_text("{}", encoding="utf-8")

    accounts = [
        {
            "email": "user@example.com",
            "status": "standby",
            "auth_file": str(other_auth_file),
            "mail_provider": "cloudmail",
            "mail_account_id": None,
        }
    ]

    monkeypatch.setattr(account_ops, "AUTH_DIR", auth_dir)
    monkeypatch.setattr(account_ops, "load_accounts", lambda: list(accounts))
    monkeypatch.setattr(account_ops, "save_accounts", lambda items: accounts.clear() or accounts.extend(items))
    monkeypatch.setattr(account_ops, "delete_account_from_configured_targets", lambda *args, **kwargs: {})
    monkeypatch.setattr(account_ops, "sync_to_cpa", lambda: None)

    cleanup = account_ops.delete_managed_account(
        "user@example.com",
        remove_remote=False,
        remove_cloudmail=False,
        sync_cpa_after=False,
    )

    assert cleanup["local_auth_files"] == []
    assert other_auth_file.exists()
    assert accounts == []
