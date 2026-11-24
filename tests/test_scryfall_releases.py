"""Test the commands to store Scryfall snapshots on GitHub."""

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from ormosbot.scryfall_releases import SNAPSHOT_ASSET, ReleaseStore, release_version

REPO = "owner/ormosbot"
SEED = "scryfall-stats-data-0.0"
TAG = "scryfall-stats-data-12"


def _release(
    version: str,
    *,
    draft: bool = True,
    asset_state: str = "uploaded",
    prerelease: bool = False,
) -> dict[str, Any]:
    return {
        "tag_name": f"scryfall-stats-data-{version}",
        "name": f"Scryfall stats data {version}",
        "draft": draft,
        "prerelease": prerelease,
        "assets": [{"name": SNAPSHOT_ASSET, "state": asset_state}]
        if asset_state
        else [],
    }


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Replace subprocess.run so the tests never use GitHub credentials."""
    command = Mock()
    monkeypatch.setattr(subprocess, "run", command)
    return command


def test_release_version() -> None:
    """Read the run number and accept the published seed."""
    assert release_version(SEED) == 0
    assert release_version(TAG) == 12


@pytest.mark.parametrize(
    "tag", ["v1.0", "scryfall-stats-data-1.1", "scryfall-stats-data-01.0"]
)
def test_invalid_release_version(tag: str) -> None:
    """Reject software releases and noncanonical data tags."""
    with pytest.raises(ValueError, match="Invalid Scryfall stats release tag"):
        release_version(tag)


def test_pending_tags_from_all_pages(gh: Mock) -> None:
    """Include newer uploaded drafts and skip incomplete or unrelated releases."""
    gh.return_value.stdout = json.dumps(
        [
            [_release("12"), _release("15", asset_state="new"), _release("9")],
            [
                _release("11"),
                _release("8", draft=False),
                _release("14", asset_state=""),
                {"tag_name": "v12", "draft": True},
            ],
        ]
    )

    pending = ReleaseStore(REPO).pending_tags("scryfall-stats-data-10")

    assert pending == ["scryfall-stats-data-11", TAG]
    gh.assert_called_once_with(
        ["gh", "api", "--paginate", "--slurp", f"repos/{REPO}/releases?per_page=100"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )


def test_pending_tags_rejects_an_ambiguous_draft(gh: Mock) -> None:
    """Fail before editing if two drafts use the same data tag."""
    gh.return_value.stdout = json.dumps([[_release("12"), _release("12")]])

    with pytest.raises(ValueError, match="Multiple Scryfall stats drafts"):
        ReleaseStore(REPO).pending_tags(SEED)


def test_pending_tags_rejects_wrong_draft_title(gh: Mock) -> None:
    """Fail before editing if the data tag and release name disagree."""
    wrong_title = {**_release("12"), "name": "Scryfall stats data 14"}
    gh.return_value.stdout = json.dumps([[wrong_title]])

    with pytest.raises(ValueError, match="Unexpected Scryfall stats draft"):
        ReleaseStore(REPO).pending_tags(SEED)


def test_pending_tags_rejects_a_superseded_baseline(gh: Mock) -> None:
    """Stop before editing when the selector returns an old data release."""
    gh.return_value.stdout = json.dumps([[_release("12", draft=False)]])
    with pytest.raises(ValueError, match="newer than"):
        ReleaseStore(REPO).pending_tags(SEED)


def test_download_and_publish(gh: Mock, tmp_path: Path) -> None:
    """Use exact tags and leave the global latest release alone."""
    store = ReleaseStore(REPO)
    destination = tmp_path / "download.json"

    store.download_snapshot(TAG, destination)
    store.publish(TAG)

    download, publish = [call.args[0] for call in gh.call_args_list]
    assert download == [
        "gh",
        "release",
        "download",
        TAG,
        "--repo",
        REPO,
        "--pattern",
        SNAPSHOT_ASSET,
        "--output",
        str(destination),
    ]
    assert publish == [
        "gh",
        "release",
        "edit",
        TAG,
        "--repo",
        REPO,
        "--draft=false",
        "--latest=false",
    ]
    assert all(call.kwargs == {"check": True} for call in gh.call_args_list)


def test_create_draft(gh: Mock, tmp_path: Path) -> None:
    """Upload the expected asset to a draft with a pinned target commit."""
    snapshot = tmp_path / SNAPSHOT_ASSET
    snapshot.write_text("{}", encoding="utf-8")
    target = "a" * 40

    ReleaseStore(REPO).create_draft(TAG, snapshot, target)

    command = gh.call_args.args[0]
    assert command[:6] == ["gh", "release", "create", TAG, "--repo", REPO]
    assert command[-1] == str(snapshot)
    assert "--draft" in command
    assert "--latest=false" in command
    assert command[command.index("--target") + 1] == target
    assert command[command.index("--title") + 1] == "Scryfall stats data 12"
    assert gh.call_args.kwargs == {"check": True}


def test_create_draft_requires_a_snapshot(gh: Mock, tmp_path: Path) -> None:
    """Refuse to create a draft for a missing, empty, or wrongly named asset."""
    store = ReleaseStore(REPO)
    snapshot = tmp_path / SNAPSHOT_ASSET
    bogus = tmp_path / "other.json"
    bogus.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="Expected a local"):
        store.create_draft(TAG, bogus, "main")
    with pytest.raises(ValueError, match="Expected a local"):
        store.create_draft(TAG, snapshot, "main")

    snapshot.touch()
    with pytest.raises(ValueError, match="file is empty"):
        store.create_draft(TAG, snapshot, "main")
    gh.assert_not_called()
