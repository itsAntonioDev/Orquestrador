"""Testes do adaptador de webhooks do GitLab."""

from __future__ import annotations

from typing import Any

import pytest

from orquestrador.projects import Provider
from orquestrador.webhooks import EventIgnored, InvalidPayload, gitlab

SHA = "c" * 40


def push_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "object_kind": "push",
        "ref": "refs/heads/develop",
        "before": "d" * 40,
        "after": SHA,
        "checkout_sha": SHA,
        "user_username": "maria",
        "user_name": "Maria",
        "project": {
            "path_with_namespace": "grupo/sub/app",
            "git_http_url": "https://gitlab.com/grupo/sub/app.git",
        },
        "commits": [{"message": "primeiro"}, {"message": "último"}],
    }
    payload.update(overrides)
    return payload


def mr_payload(action: str = "open", **attributes: Any) -> dict[str, Any]:
    return {
        "object_kind": "merge_request",
        "user": {"username": "joao"},
        "project": {
            "path_with_namespace": "grupo/sub/app",
            "git_http_url": "https://gitlab.com/grupo/sub/app.git",
        },
        "object_attributes": {
            "action": action,
            "iid": 7,
            "title": "Melhoria",
            "source_branch": "feature/y",
            "target_branch": "main",
            "last_commit": {"id": "e" * 40},
            "source": {"git_http_url": "https://gitlab.com/fork/app.git"},
            **attributes,
        },
    }


class TestToken:
    def test_valid_token(self) -> None:
        assert gitlab.verify_token("token", "token")

    @pytest.mark.parametrize("token", [None, "", "outro", "token "])
    def test_invalid_token(self, token: str | None) -> None:
        assert not gitlab.verify_token("token", token)

    def test_empty_secret_never_validates(self) -> None:
        assert not gitlab.verify_token("", "")

    def test_adapter_reads_header(self) -> None:
        assert gitlab.ADAPTER.verify("t", b"", {gitlab.TOKEN_HEADER: "t"})
        assert not gitlab.ADAPTER.verify("t", b"", {})


class TestParsePush:
    def test_branch_push(self) -> None:
        event = gitlab.parse_event("Push Hook", push_payload(), "uuid-1")
        assert event.provider == Provider.GITLAB
        assert event.event == "push"
        assert event.repository == "grupo/sub/app"
        assert event.branch == "develop"
        assert event.commit == SHA
        assert event.actor == "maria"
        assert event.message == "último"
        assert event.clone_url == "https://gitlab.com/grupo/sub/app.git"
        assert event.delivery_id == "uuid-1"

    def test_tag_push(self) -> None:
        event = gitlab.parse_event("Tag Push Hook", push_payload(ref="refs/tags/v2.0"))
        assert event.tag == "v2.0"
        assert event.branch is None

    def test_uses_after_when_checkout_sha_missing(self) -> None:
        event = gitlab.parse_event("Push Hook", push_payload(checkout_sha=None))
        assert event.commit == SHA

    def test_deleted_branch_is_ignored(self) -> None:
        payload = push_payload(after=gitlab.NULL_SHA, checkout_sha=None)
        with pytest.raises(EventIgnored, match="remoção"):
            gitlab.parse_event("Push Hook", payload)

    def test_missing_ref(self) -> None:
        payload = push_payload()
        del payload["ref"]
        with pytest.raises(InvalidPayload, match="ref"):
            gitlab.parse_event("Push Hook", payload)

    def test_actor_fallback_and_no_commits(self) -> None:
        event = gitlab.parse_event("Push Hook", push_payload(user_username=None, commits=[]))
        assert event.actor == "Maria"
        assert event.message is None


class TestParseMergeRequest:
    @pytest.mark.parametrize("action", ["open", "reopen"])
    def test_open_and_reopen(self, action: str) -> None:
        event = gitlab.parse_event("Merge Request Hook", mr_payload(action))
        assert event.event == "pull_request"
        assert event.commit == "e" * 40
        assert event.branch == "feature/y"
        assert event.base_branch == "main"
        assert event.pull_request == 7
        assert event.ref == "refs/merge-requests/7/head"
        assert event.clone_url == "https://gitlab.com/fork/app.git"
        assert event.actor == "joao"
        assert event.message == "Melhoria"

    def test_update_with_new_commits(self) -> None:
        event = gitlab.parse_event("Merge Request Hook", mr_payload("update", oldrev="f" * 40))
        assert event.commit == "e" * 40

    def test_update_without_new_commits_is_ignored(self) -> None:
        with pytest.raises(EventIgnored, match="sem novos commits"):
            gitlab.parse_event("Merge Request Hook", mr_payload("update"))

    @pytest.mark.parametrize("action", ["close", "merge", "approved"])
    def test_irrelevant_actions(self, action: str) -> None:
        with pytest.raises(EventIgnored):
            gitlab.parse_event("Merge Request Hook", mr_payload(action))

    def test_missing_attributes(self) -> None:
        with pytest.raises(InvalidPayload):
            gitlab.parse_event("Merge Request Hook", {"project": {}})

    def test_missing_last_commit(self) -> None:
        with pytest.raises(InvalidPayload, match="last_commit"):
            gitlab.parse_event("Merge Request Hook", mr_payload(last_commit=None))


def test_unsupported_event() -> None:
    with pytest.raises(EventIgnored, match="Issue Hook"):
        gitlab.parse_event("Issue Hook", {})
