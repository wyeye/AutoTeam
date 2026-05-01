"""账号资源清理与远端对账操作。"""

import json
import logging
from pathlib import Path

from autoteam.accounts import find_account, load_accounts, save_accounts
from autoteam.admin_state import get_chatgpt_account_id
from autoteam.mail_provider import get_account_mail_account_id, get_account_mail_provider, get_mail_client
from autoteam.sync_targets import delete_account_from_configured_targets
from autoteam.sync_targets import sync_to_configured_targets as sync_to_cpa

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
AUTH_DIR = PROJECT_ROOT / "auths"


def _normalized_email(value):
    return (value or "").strip().lower()


def _find_account_case_insensitive(accounts, email):
    email_l = _normalized_email(email)
    for acc in accounts:
        if _normalized_email(acc.get("email")) == email_l:
            return acc
    return None


def _is_safe_auth_file_path(path, email=None):
    """Only allow account deletion to unlink auth files owned by AutoTeam."""
    try:
        resolved_path = Path(path).expanduser().resolve(strict=False)
        resolved_auth_dir = AUTH_DIR.expanduser().resolve(strict=False)
        resolved_path.relative_to(resolved_auth_dir)
    except Exception:
        return False

    if resolved_path.suffix != ".json":
        return False

    name = resolved_path.name.lower()
    email_l = _normalized_email(email)
    if email_l:
        return name.startswith(f"codex-{email_l}-")
    return name.startswith("codex-") and not name.startswith("codex-main-")


def _auth_files_for_email(email, acc=None):
    """Return safe local auth-file candidates for an account email."""
    email_l = _normalized_email(email)
    candidates = set()

    if acc and acc.get("auth_file"):
        auth_path = Path(acc["auth_file"])
        if _is_safe_auth_file_path(auth_path, email_l):
            candidates.add(auth_path)
        else:
            logger.warning("[账号] 跳过不安全的 auth_file 路径: %s", auth_path)

    prefix = f"codex-{email_l}-"
    for path in AUTH_DIR.glob("codex-*.json"):
        if path.name.lower().startswith(prefix) and _is_safe_auth_file_path(path, email_l):
            candidates.add(path)

    return candidates


def _response_excerpt(body, limit=240):
    text = str(body or "").strip().replace("\n", " ")
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


def _parse_team_api_json(response, label):
    status = int(response.get("status") or 0)
    body = response.get("body", "")

    if status in (401, 403):
        raise RuntimeError(f"{label}接口鉴权失败 (HTTP {status})，请重新完成管理员登录")
    if status != 200:
        raise RuntimeError(f"{label}接口请求失败 (HTTP {status}): {_response_excerpt(body)}")

    try:
        return json.loads(body)
    except Exception as exc:
        lower_body = str(body or "").lower()
        if "<html" in lower_body or "<!doctype" in lower_body:
            raise RuntimeError(f"{label}接口返回了非 JSON 内容（疑似登录页或错误页），请重新完成管理员登录") from exc
        raise RuntimeError(f"{label}接口返回了非 JSON 内容: {_response_excerpt(body)}") from exc


def _record_cleanup_error(cleanup, layer, message, *, detail=None):
    """Record a cleanup-layer failure without aborting the delete flow."""
    cleanup["partial_failure"] = True
    error = {
        "layer": layer,
        "message": str(message),
    }
    if detail is not None:
        error["detail"] = _response_excerpt(detail)
    cleanup["errors"].append(error)


def fetch_team_state(chatgpt_api):
    """读取 Team 成员和邀请状态。"""
    account_id = get_chatgpt_account_id()
    members = []
    invites = []

    users_resp = chatgpt_api._api_fetch("GET", f"/backend-api/accounts/{account_id}/users")
    data = _parse_team_api_json(users_resp, "Team 成员")
    members = data.get("items", data.get("users", data.get("members", [])))

    invites_resp = chatgpt_api._api_fetch("GET", f"/backend-api/accounts/{account_id}/invites")
    data = _parse_team_api_json(invites_resp, "Team 邀请")
    invites = data if isinstance(data, list) else data.get("invites", data.get("account_invites", []))

    return members, invites


def _fetch_team_state_best_effort(chatgpt_api, account_id, cleanup):
    """读取 Team 状态；成员/邀请任一失败都不阻断另一侧。"""
    members = []
    invites = []

    try:
        users_resp = chatgpt_api._api_fetch("GET", f"/backend-api/accounts/{account_id}/users")
        data = _parse_team_api_json(users_resp, "Team 成员")
        members = data.get("items", data.get("users", data.get("members", [])))
    except Exception as exc:
        logger.warning("[账号] 读取 Team 成员失败，继续尝试读取邀请: %s", exc)
        _record_cleanup_error(cleanup, "team_members_state", f"读取 Team 成员失败: {exc}")

    try:
        invites_resp = chatgpt_api._api_fetch("GET", f"/backend-api/accounts/{account_id}/invites")
        data = _parse_team_api_json(invites_resp, "Team 邀请")
        invites = data if isinstance(data, list) else data.get("invites", data.get("account_invites", []))
    except Exception as exc:
        logger.warning("[账号] 读取 Team 邀请失败，继续执行其它删除层: %s", exc)
        _record_cleanup_error(cleanup, "team_invites_state", f"读取 Team 邀请失败: {exc}")

    return members, invites


def delete_managed_account(
    email,
    *,
    remove_remote=True,
    remove_cloudmail=True,
    sync_cpa_after=True,
    strict_cloudmail=False,
    chatgpt_api=None,
    mail_client=None,
    remote_state=None,
    include_disabled_sync_targets=True,
):
    """
    删除本地管理账号及其衍生资源。
    返回 cleanup 摘要，设计为幂等操作。
    """
    email_l = _normalized_email(email)
    accounts = load_accounts()
    acc = find_account(accounts, email) or _find_account_case_insensitive(accounts, email)

    cleanup = {
        "local_record": False,
        "local_auth_files": [],
        "cpa_files": [],
        "sub2api_accounts": [],
        "team_member_removed": False,
        "invite_removed": False,
        "cloudmail_deleted": False,
        "partial_failure": False,
        "errors": [],
    }

    members = []
    invites = []
    own_chatgpt = None
    own_mail_client = None

    try:
        if remove_remote:
            account_id = None
            try:
                account_id = get_chatgpt_account_id()
                if remote_state is not None:
                    members, invites = remote_state
                else:
                    if chatgpt_api is None:
                        from autoteam.chatgpt_api import ChatGPTTeamAPI

                        own_chatgpt = ChatGPTTeamAPI()
                        own_chatgpt.start()
                        chatgpt_api = own_chatgpt
                    members, invites = _fetch_team_state_best_effort(chatgpt_api, account_id, cleanup)
            except Exception as exc:
                logger.warning("[账号] 获取 Team 状态失败，继续执行其它删除层: %s", exc)
                _record_cleanup_error(cleanup, "team_state", f"获取 Team 状态失败: {exc}")

            if account_id and chatgpt_api is not None:
                member_matches = [m for m in members if (m.get("email", "") or "").lower() == email_l]
                for member in member_matches:
                    user_id = member.get("user_id") or member.get("id")
                    if not user_id:
                        continue
                    try:
                        result = chatgpt_api._api_fetch(
                            "DELETE",
                            f"/backend-api/accounts/{account_id}/users/{user_id}",
                        )
                        if result["status"] not in (200, 204):
                            raise RuntimeError(
                                f"移除 Team 成员失败: HTTP {result.get('status')} {_response_excerpt(result.get('body'))}"
                            )
                        cleanup["team_member_removed"] = True
                    except Exception as exc:
                        logger.warning("[账号] 移除 Team 成员失败，继续执行其它删除层: %s", exc)
                        _record_cleanup_error(cleanup, "team_member", f"移除 Team 成员失败: {exc}")

                invite_matches = []
                for inv in invites:
                    inv_email = (inv.get("email_address") or inv.get("email") or "").lower()
                    if inv_email == email_l:
                        invite_matches.append(inv)

                for inv in invite_matches:
                    invite_id = inv.get("id")
                    if not invite_id:
                        continue
                    try:
                        result = chatgpt_api._api_fetch(
                            "DELETE",
                            f"/backend-api/accounts/{account_id}/invites/{invite_id}",
                        )
                        if result["status"] not in (200, 204):
                            raise RuntimeError(
                                f"取消 Team 邀请失败: HTTP {result.get('status')} {_response_excerpt(result.get('body'))}"
                            )
                        cleanup["invite_removed"] = True
                    except Exception as exc:
                        logger.warning("[账号] 取消 Team 邀请失败，继续执行其它删除层: %s", exc)
                        _record_cleanup_error(cleanup, "team_invite", f"取消 Team 邀请失败: {exc}")

        try:
            auth_candidates = _auth_files_for_email(email_l, acc)
        except Exception as exc:
            auth_candidates = []
            logger.warning("[账号] 收集本地 auth 文件失败，继续执行其它删除层: %s", exc)
            _record_cleanup_error(cleanup, "local_auth_scan", f"收集本地 auth 文件失败: {exc}")

        for path in sorted(auth_candidates):
            try:
                if path.exists():
                    path.unlink()
                    cleanup["local_auth_files"].append(path.name)
                    logger.info("[账号] 已删除本地 auth: %s", path.name)
            except Exception as exc:
                logger.warning("[账号] 删除本地 auth 失败，继续执行其它删除层: %s", exc)
                _record_cleanup_error(cleanup, "local_auth_file", f"删除本地 auth 失败: {path.name}: {exc}")

        if remove_remote:
            try:
                remote_cleanup = delete_account_from_configured_targets(
                    email_l,
                    auth_names=list(cleanup["local_auth_files"]),
                    include_disabled=include_disabled_sync_targets,
                )
                cpa_cleanup = remote_cleanup.get("cpa") or {}
                sub2api_cleanup = remote_cleanup.get("sub2api") or {}
                cleanup["cpa_files"] = list(cpa_cleanup.get("deleted", []))
                cleanup["sub2api_accounts"] = list(sub2api_cleanup.get("deleted", []))
                for layer, result in (("cpa", cpa_cleanup), ("sub2api", sub2api_cleanup)):
                    if result.get("error"):
                        _record_cleanup_error(cleanup, layer, f"{layer} 删除失败: {result['error']}")
            except Exception as exc:
                logger.warning("[账号] 删除配置远端目标失败，继续执行其它删除层: %s", exc)
                _record_cleanup_error(cleanup, "sync_targets", f"删除配置远端目标失败: {exc}")

        if acc:
            try:
                mail_account_id = get_account_mail_account_id(acc)
            except Exception as exc:
                mail_account_id = None
                logger.warning("[账号] 读取邮箱账户 ID 失败，继续执行其它删除层: %s", exc)
                _record_cleanup_error(cleanup, "cloudmail", f"读取邮箱账户 ID 失败: {exc}")
            if remove_cloudmail and mail_account_id is not None:
                try:
                    provider = get_account_mail_provider(acc)
                    if mail_client is None or getattr(mail_client, "provider_name", "") != provider:
                        own_mail_client = get_mail_client(provider)
                        own_mail_client.login()
                        mail_client = own_mail_client
                    resp = mail_client.delete_account(mail_account_id)
                    if resp.get("code") == 200:
                        cleanup["cloudmail_deleted"] = True
                    else:
                        message = f"邮箱提供者返回异常: {resp}"
                        if strict_cloudmail:
                            logger.warning("[账号] %s，继续执行其它删除层", message)
                        _record_cleanup_error(cleanup, "cloudmail", message)
                except Exception as exc:
                    logger.warning("[账号] 删除邮箱提供者账户失败: %s", exc)
                    _record_cleanup_error(cleanup, "cloudmail", f"删除邮箱提供者账户失败: {exc}")

            try:
                accounts = [item for item in accounts if item["email"].lower() != email_l]
                save_accounts(accounts)
                cleanup["local_record"] = True
                logger.info("[账号] 已删除本地记录: %s", email)
            except Exception as exc:
                logger.error("[账号] 删除本地记录失败: %s", exc)
                _record_cleanup_error(cleanup, "local_record", f"删除本地记录失败: {exc}")

        if remove_remote and sync_cpa_after:
            try:
                sync_to_cpa()
            except Exception as exc:
                logger.warning("[账号] 删除后的远端同步失败: %s", exc)
                _record_cleanup_error(cleanup, "sync_after", f"删除后的远端同步失败: {exc}")

        return cleanup
    finally:
        if own_chatgpt:
            try:
                own_chatgpt.stop()
            except Exception as exc:
                logger.warning("[账号] 关闭 Team 会话失败: %s", exc)


def delete_managed_account_hard(email, **kwargs):
    """Delete a dashboard-managed account and all associated resources."""
    kwargs.setdefault("include_disabled_sync_targets", True)
    kwargs.update(
        {
            "remove_remote": True,
            "remove_cloudmail": True,
            "sync_cpa_after": True,
            "strict_cloudmail": True,
        }
    )
    return delete_managed_account(email, **kwargs)
