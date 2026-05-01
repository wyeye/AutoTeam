import logging

from autoteam import manager


class _FakeMailClient:
    provider_name = "cloudmail"

    def login(self):
        return None


def test_record_auth_repair_failure_pauses_on_add_phone(monkeypatch):
    updates = []
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [{"email": "user@example.com", "auth_retry_count": 1}],
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(manager.time, "time", lambda: 1_700_000_000)

    state = manager._record_auth_repair_failure("user@example.com", "add_phone", "需要手机号验证")

    assert state["auth_retry_paused"] is True
    assert state["auth_retry_after"] is None
    assert updates == [
        (
            "user@example.com",
            {
                "auth_retry_count": 3,
                "auth_last_error": "add_phone",
                "auth_last_error_detail": "需要手机号验证",
                "auth_last_failed_at": 1_700_000_000,
                "auth_retry_after": None,
                "auth_retry_paused": True,
            },
        )
    ]


def test_record_auth_repair_failure_uses_auto_check_interval_backoff(monkeypatch):
    updates = []
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [{"email": "user@example.com", "auth_retry_count": 0}],
    )
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(manager.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(manager, "_auth_repair_retry_delays", lambda: (600, 1200, 1800))

    state = manager._record_auth_repair_failure("user@example.com", "auth_code_missing", "未获取到 auth code")

    assert state["auth_retry_count"] == 1
    assert state["auth_retry_after"] == 1_700_000_600
    assert updates == [
        (
            "user@example.com",
            {
                "auth_retry_count": 1,
                "auth_last_error": "auth_code_missing",
                "auth_last_error_detail": "未获取到 auth code",
                "auth_last_failed_at": 1_700_000_000,
                "auth_retry_after": 1_700_000_600,
                "auth_retry_paused": False,
            },
        )
    ]


def test_login_codex_with_result_retries_retryable_failures_within_same_round(monkeypatch):
    attempts = {"count": 0}

    def fake_login(email, password, mail_client=None, return_result=False):
        assert return_result is True
        attempts["count"] += 1
        if attempts["count"] < 3:
            return {
                "ok": False,
                "bundle": None,
                "error_type": "auth_code_missing",
                "error_detail": "未获取到 auth code",
                "retryable": True,
            }
        return {
            "ok": True,
            "bundle": {"email": email, "plan_type": "team"},
            "error_type": None,
            "error_detail": None,
            "retryable": False,
        }

    monkeypatch.setattr(manager, "login_codex_via_browser", fake_login)

    result = manager._login_codex_with_result("user@example.com", "", max_attempts=3)

    assert attempts["count"] == 3
    assert result["ok"] is True
    assert result["bundle"]["plan_type"] == "team"
    assert result["attempts"] == 3


def test_login_codex_with_result_stops_immediately_on_hard_failure(monkeypatch):
    attempts = {"count": 0}

    def fake_login(email, password, mail_client=None, return_result=False):
        assert return_result is True
        attempts["count"] += 1
        return {
            "ok": False,
            "bundle": None,
            "error_type": "add_phone",
            "error_detail": "需要手机号验证",
            "retryable": False,
        }

    monkeypatch.setattr(manager, "login_codex_via_browser", fake_login)

    result = manager._login_codex_with_result("user@example.com", "", max_attempts=3)

    assert attempts["count"] == 1
    assert result["ok"] is False
    assert result["error_type"] == "add_phone"
    assert result["attempts"] == 1


def test_login_codex_with_result_rejects_non_team_bundle(monkeypatch):
    def fake_login(email, password, mail_client=None, return_result=False):
        assert return_result is True
        return {
            "ok": True,
            "bundle": {"email": email, "plan_type": "free"},
            "error_type": None,
            "error_detail": None,
            "retryable": False,
        }

    monkeypatch.setattr(manager, "login_codex_via_browser", fake_login)

    result = manager._login_codex_with_result("user@example.com", "", max_attempts=1)

    assert result["ok"] is False
    assert result["bundle"] is None
    assert result["error_type"] == "non_team_plan"
    assert result["attempts"] == 1


def test_hard_auth_failure_helper_deletes_with_full_cleanup(monkeypatch):
    calls = []
    mail_client = _FakeMailClient()
    chatgpt = object()

    monkeypatch.setattr(manager, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(manager, "_chatgpt_session_ready", lambda api: api is chatgpt)
    monkeypatch.setattr(
        manager,
        "delete_managed_account_hard",
        lambda email, **kwargs: calls.append((email, kwargs)) or {"local_record": True},
    )

    deleted = manager._delete_account_after_hard_auth_failure(
        "user@example.com",
        "add_phone",
        chatgpt_api=chatgpt,
        mail_client=mail_client,
        reason="需要手机号验证",
    )

    assert deleted is True
    assert calls == [("user@example.com", {"chatgpt_api": chatgpt, "include_disabled_sync_targets": False, "mail_client": mail_client})]


def test_hard_auth_failure_helper_skips_retryable_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(manager, "delete_managed_account_hard", lambda *args, **kwargs: calls.append((args, kwargs)))

    deleted = manager._delete_account_after_hard_auth_failure(
        "user@example.com",
        "auth_code_missing",
        reason="未获取到 auth code",
    )

    assert deleted is False
    assert calls == []


def test_email_verification_failure_requires_delete_when_retry_after_is_at_least_18_minutes():
    state = {
        "auth_last_failed_at": 1_700_000_000,
        "auth_retry_after": 1_700_001_080,
    }

    assert manager._auth_failure_requires_delete("email_verification", state) is True


def test_email_verification_failure_does_not_delete_before_18_minutes():
    state = {
        "auth_last_failed_at": 1_700_000_000,
        "auth_retry_after": 1_700_001_020,
    }

    assert manager._auth_failure_requires_delete("email_verification", state) is False


def test_cmd_check_auto_deletes_auth_pending_on_add_phone(monkeypatch):
    calls = []
    updates = []
    mail_client = _FakeMailClient()

    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "pending@example.com",
                "status": "auth_pending",
                "password": "",
                "auth_file": None,
                "mail_provider": "cloudmail",
                "auth_retry_count": 0,
                "auth_last_error": None,
                "auth_retry_after": None,
                "auth_retry_paused": False,
            }
        ],
    )
    monkeypatch.setattr(manager, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(manager, "get_mail_domain", lambda: "@example.com")
    monkeypatch.setattr(manager, "_get_account_mail_client", lambda _acc: mail_client)
    monkeypatch.setattr(manager, "_is_email_in_team", lambda _email: True)
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(manager.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(
        manager,
        "_login_codex_with_result",
        lambda email, password, mail_client=None: {
            "ok": False,
            "bundle": None,
            "error_type": "add_phone",
            "error_detail": "需要手机号验证",
            "retryable": False,
        },
    )
    monkeypatch.setattr(
        manager,
        "delete_managed_account_hard",
        lambda email, **kwargs: calls.append((email, kwargs)) or {"local_record": True, "team_member_removed": True},
    )

    exhausted = manager.cmd_check(force_auth_repair=True)

    assert exhausted == []
    assert calls == [("pending@example.com", {"chatgpt_api": None, "include_disabled_sync_targets": False, "mail_client": mail_client})]
    assert updates[-1][0] == "pending@example.com"
    assert updates[-1][1]["auth_last_error"] == "add_phone"


def test_cmd_check_auto_deletes_auth_pending_on_email_verification_18_minute_retry(monkeypatch):
    calls = []
    updates = []
    mail_client = _FakeMailClient()

    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "pending@example.com",
                "status": "auth_pending",
                "password": "",
                "auth_file": None,
                "mail_provider": "cloudmail",
                "auth_retry_count": 0,
                "auth_last_error": None,
                "auth_retry_after": None,
                "auth_retry_paused": False,
            }
        ],
    )
    monkeypatch.setattr(manager, "_auth_repair_retry_delays", lambda: (1080, 1200, 1800))
    monkeypatch.setattr(manager, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(manager, "get_mail_domain", lambda: "@example.com")
    monkeypatch.setattr(manager, "_get_account_mail_client", lambda _acc: mail_client)
    monkeypatch.setattr(manager, "_is_email_in_team", lambda _email: True)
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(manager.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(
        manager,
        "_login_codex_with_result",
        lambda email, password, mail_client=None: {
            "ok": False,
            "bundle": None,
            "error_type": "email_verification",
            "error_detail": "卡在邮箱验证码页",
            "retryable": True,
        },
    )
    monkeypatch.setattr(
        manager,
        "delete_managed_account_hard",
        lambda email, **kwargs: calls.append((email, kwargs)) or {"local_record": True, "team_member_removed": True},
    )

    exhausted = manager.cmd_check(force_auth_repair=True)

    assert exhausted == []
    assert calls == [("pending@example.com", {"chatgpt_api": None, "include_disabled_sync_targets": False, "mail_client": mail_client})]
    assert updates[-1][0] == "pending@example.com"
    assert updates[-1][1]["auth_last_error"] == "email_verification"
    assert updates[-1][1]["auth_retry_after"] == 1_700_001_080


def test_reinvite_account_auto_deletes_on_add_phone(monkeypatch):
    calls = []
    updates = []
    mail_client = _FakeMailClient()

    monkeypatch.setattr(manager, "_chatgpt_session_ready", lambda _api: False)
    monkeypatch.setattr(manager, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(manager, "_is_email_in_team", lambda _email: True)
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(manager.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(
        manager,
        "_login_codex_with_result",
        lambda email, password, mail_client=None: {
            "ok": False,
            "bundle": None,
            "error_type": "add_phone",
            "error_detail": "需要手机号验证",
            "retryable": False,
        },
    )
    monkeypatch.setattr(
        manager,
        "delete_managed_account_hard",
        lambda email, **kwargs: calls.append((email, kwargs)) or {"local_record": True, "team_member_removed": True},
    )

    result = manager.reinvite_account(
        object(),
        mail_client,
        {"email": "tmp-user@example.com", "password": ""},
    )

    assert result is False
    assert calls == [("tmp-user@example.com", {"chatgpt_api": None, "include_disabled_sync_targets": False, "mail_client": mail_client})]
    assert updates[-1][1]["auth_last_error"] == "add_phone"


def test_complete_registration_auto_deletes_on_add_phone(monkeypatch):
    import playwright.sync_api as playwright_sync_api

    import autoteam.invite as invite

    class _FakeBrowser:
        def new_context(self, **_kwargs):
            return self

        def new_page(self):
            return object()

        def close(self):
            return None

    class _FakeChromium:
        def launch(self, **_kwargs):
            return _FakeBrowser()

    class _FakePlaywrightContext:
        chromium = _FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    calls = []
    updates = []
    mail_client = _FakeMailClient()

    monkeypatch.setattr(playwright_sync_api, "sync_playwright", lambda: _FakePlaywrightContext())
    monkeypatch.setattr(invite, "register_with_invite", lambda *args, **kwargs: (True, "secret"))
    monkeypatch.setattr(manager, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(manager, "load_accounts", lambda: [{"email": "invite-user@example.com", "auth_retry_count": 0}])
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(manager.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(
        manager,
        "_login_codex_with_result",
        lambda email, password, mail_client=None: {
            "ok": False,
            "bundle": None,
            "error_type": "add_phone",
            "error_detail": "需要手机号验证",
            "retryable": False,
        },
    )
    monkeypatch.setattr(
        manager,
        "delete_managed_account_hard",
        lambda email, **kwargs: calls.append((email, kwargs)) or {"local_record": True, "invite_removed": True},
    )

    result = manager._complete_registration(
        "invite-user@example.com",
        "initial",
        "https://invite.example",
        mail_client,
    )

    assert result is None
    assert calls == [("invite-user@example.com", {"chatgpt_api": None, "include_disabled_sync_targets": False, "mail_client": mail_client})]
    assert updates[-1][1]["auth_last_error"] == "add_phone"


def test_create_account_direct_auto_deletes_on_add_phone(monkeypatch):
    class _DirectMailClient(_FakeMailClient):
        def create_temp_email(self):
            return 42, "new-user@example.com"

    calls = []
    updates = []
    added = []
    mail_client = _DirectMailClient()

    monkeypatch.setattr(manager, "_register_direct_once", lambda *args, **kwargs: True)
    monkeypatch.setattr(manager, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(manager, "load_accounts", lambda: [{"email": "new-user@example.com", "auth_retry_count": 0}])
    monkeypatch.setattr(manager, "add_account", lambda *args, **kwargs: added.append((args, kwargs)))
    monkeypatch.setattr(manager, "update_account", lambda email, **kwargs: updates.append((email, kwargs)))
    monkeypatch.setattr(manager.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(
        manager,
        "_login_codex_with_result",
        lambda email, password, mail_client=None: {
            "ok": False,
            "bundle": None,
            "error_type": "add_phone",
            "error_detail": "需要手机号验证",
            "retryable": False,
        },
    )
    monkeypatch.setattr(
        manager,
        "delete_managed_account_hard",
        lambda email, **kwargs: calls.append((email, kwargs)) or {"local_record": True, "team_member_removed": True},
    )

    result = manager.create_account_direct(mail_client)

    assert result is None
    assert added[0][0][0] == "new-user@example.com"
    assert added[0][1] == {
        "cloudmail_account_id": 42,
        "mail_provider": "cloudmail",
        "mail_account_id": 42,
    }
    assert calls == [("new-user@example.com", {"chatgpt_api": None, "include_disabled_sync_targets": False, "mail_client": mail_client})]
    assert updates[-1][1]["auth_last_error"] == "add_phone"


def test_cmd_check_skips_cooled_down_auth_pending_account(monkeypatch, caplog):
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "pending@example.com",
                "status": "auth_pending",
                "auth_file": None,
                "mail_provider": "cloudmail",
                "auth_retry_count": 1,
                "auth_last_error": "auth_code_missing",
                "auth_retry_after": 1_700_000_600,
                "auth_retry_paused": False,
            }
        ],
    )
    monkeypatch.setattr(manager.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(manager, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(manager, "get_mail_domain", lambda: "@example.com")
    monkeypatch.setattr(
        manager,
        "_login_codex_with_result",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not attempt login during cooldown")),
    )

    with caplog.at_level(logging.INFO):
        exhausted = manager.cmd_check(force_auth_repair=False)

    assert exhausted == []
    assert "跳过 1 个处于冷却/暂停中的认证修复账号" in caplog.text
    assert "pending@example.com（自动修复冷却中" in caplog.text


def test_cmd_check_force_auth_repair_ignores_cooldown(monkeypatch):
    calls = []
    monkeypatch.setattr(
        manager,
        "load_accounts",
        lambda: [
            {
                "email": "pending@example.com",
                "status": "auth_pending",
                "password": "",
                "auth_file": None,
                "mail_provider": "cloudmail",
                "auth_retry_count": 2,
                "auth_last_error": "auth_code_missing",
                "auth_retry_after": 1_700_000_600,
                "auth_retry_paused": False,
            }
        ],
    )
    monkeypatch.setattr(manager.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(manager, "_is_main_account_email", lambda _email: False)
    monkeypatch.setattr(manager, "get_mail_domain", lambda: "@example.com")
    monkeypatch.setattr(manager, "_get_account_mail_client", lambda _acc: _FakeMailClient())
    monkeypatch.setattr(
        manager,
        "_login_codex_with_result",
        lambda email, password, mail_client=None: (
            calls.append((email, password, mail_client.provider_name))
            or {
                "ok": False,
                "bundle": None,
                "error_type": "auth_code_missing",
                "error_detail": "未获取到 auth code",
                "retryable": True,
            }
        ),
    )
    monkeypatch.setattr(manager, "_is_email_in_team", lambda _email: True)
    monkeypatch.setattr(manager, "update_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(manager, "_record_auth_repair_failure", lambda *args, **kwargs: {})

    manager.cmd_check(force_auth_repair=True)

    assert calls == [("pending@example.com", "", "cloudmail")]
