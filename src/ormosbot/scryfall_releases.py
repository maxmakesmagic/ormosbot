"""Store combined Scryfall stats snapshots in GitHub releases."""

import json
import re
import subprocess
from pathlib import Path

SNAPSHOT_ASSET = "scryfall-stats.json"
_TAG = re.compile(r"scryfall-stats-data-(0\.0|[1-9][0-9]*)")


def release_version(tag: str) -> int:
    """Extract the workflow run number, or zero for the published seed."""
    match = _TAG.fullmatch(tag)
    if match is None:
        raise ValueError(f"Invalid Scryfall stats release tag: {tag!r}")
    return 0 if match[1] == "0.0" else int(match[1])


def _release_title(tag: str) -> str:
    run = release_version(tag)
    number = "0.0" if run == 0 else str(run)
    return f"Scryfall stats data {number}"


class ReleaseStore:
    """Read, stage, and publish data releases for one repository."""

    def __init__(self, repo: str) -> None:
        """Use the GitHub CLI credentials for this repository."""
        self.repo = repo

    def pending_tags(self, baseline_tag: str) -> list[str]:
        """List newer drafts that contain a complete snapshot, oldest first."""
        baseline = release_version(baseline_tag)
        result = subprocess.run(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{self.repo}/releases?per_page=100",
            ],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        )
        tags = []
        seen = set()
        for page in json.loads(result.stdout):
            for release in page:
                tag = release["tag_name"]
                if not _TAG.fullmatch(tag) or release_version(tag) <= baseline:
                    continue
                valid_name = release["name"] == _release_title(tag)
                if not release["draft"]:
                    if valid_name and not release["prerelease"]:
                        raise ValueError(
                            f"Published Scryfall stats release {tag!r} is newer than {baseline_tag!r}"
                        )
                    continue
                if not valid_name or release["prerelease"]:
                    raise ValueError(f"Unexpected Scryfall stats draft for tag {tag!r}")
                if tag in seen:
                    raise ValueError(f"Multiple Scryfall stats drafts for tag {tag!r}")
                seen.add(tag)
                for asset in release["assets"]:
                    if asset["name"] == SNAPSHOT_ASSET and asset["state"] == "uploaded":
                        tags.append(tag)
                        break
                # A failed upload can leave an empty draft. The publisher
                # starts wiki edits only after the draft's snapshot is uploaded.
        return sorted(tags, key=release_version)

    def download_snapshot(self, tag: str, destination: Path) -> None:
        """Download the snapshot from a known data release or draft."""
        release_version(tag)
        subprocess.run(
            [
                "gh",
                "release",
                "download",
                tag,
                "--repo",
                self.repo,
                "--pattern",
                SNAPSHOT_ASSET,
                "--output",
                str(destination.resolve()),
            ],
            check=True,
        )

    def create_draft(self, tag: str, snapshot: Path, target_commit: str) -> None:
        """Upload the snapshot in a draft release before any wiki edits."""
        title = _release_title(tag)
        if snapshot.name != SNAPSHOT_ASSET or not snapshot.is_file():
            raise ValueError(f"Expected a local {SNAPSHOT_ASSET} file")
        if not snapshot.stat().st_size:
            raise ValueError(f"The {SNAPSHOT_ASSET} file is empty")
        subprocess.run(
            [
                "gh",
                "release",
                "create",
                tag,
                "--repo",
                self.repo,
                "--draft",
                "--latest=false",
                "--target",
                target_commit,
                "--title",
                title,
                "--notes",
                "Combined Scryfall query counts.",
                str(snapshot.resolve()),
            ],
            check=True,
        )

    def publish(self, tag: str) -> None:
        """Publish the data release after the wiki edits succeed."""
        release_version(tag)
        subprocess.run(
            [
                "gh",
                "release",
                "edit",
                tag,
                "--repo",
                self.repo,
                "--draft=false",
                "--latest=false",
            ],
            check=True,
        )
