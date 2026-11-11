"""Report pages with missing Scryfall data after the nightly wiki update."""

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path

import pywikibot
import requests
from pywikibot.site import BaseSite

from ormosbot.site import get_site

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.json"
CATEGORY_TITLE = "Category:Scryfall data missing"
ISSUE_TITLE = "Scryfall data missing after nightly gathering"
ISSUE_MARKER = "<!-- ormosbot:scryfall-data-missing -->"
ASSIGNEE = "maxmakesmagic"


@dataclass(frozen=True)
class MissingDataIssue:
    """The existing report and its current assignees."""

    number: int
    body: str
    assignees: tuple[str, ...]


@dataclass
class MissingScryfallDataReporter:
    """Read the wiki category and maintain one open GitHub issue."""

    site: BaseSite
    session: requests.Session
    repository: str
    run_url: str | None = None

    @property
    def issues_url(self) -> str:
        """The GitHub endpoint for issues in this repository."""
        return f"https://api.github.com/repos/{self.repository}/issues"

    def find_open_issue(self) -> MissingDataIssue | None:
        """Find the report across all pages of open issues."""
        page_number = 1
        while True:
            params: dict[str, str | int] = {
                "state": "open",
                "per_page": 100,
                "page": page_number,
            }
            response = self.session.get(
                self.issues_url,
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            issues = response.json()
            for issue in issues:
                if "pull_request" in issue:
                    continue
                body = issue.get("body") or ""
                if ISSUE_MARKER in body or issue["title"] == ISSUE_TITLE:
                    return MissingDataIssue(
                        number=issue["number"],
                        body=body,
                        assignees=tuple(user["login"] for user in issue["assignees"]),
                    )
            if len(issues) < 100:
                return None
            page_number += 1

    def render_body(
        self, category: pywikibot.Category, pages: list[pywikibot.Page]
    ) -> str:
        """List the remaining pages and link to the completed workflow run."""
        lines = [
            ISSUE_MARKER,
            "",
            "The Scryfall data gathering and wiki update finished.",
            "",
            f"Pages still in [{CATEGORY_TITLE}](<{category.full_url()}>):",
            "",
        ]
        for page in sorted(pages, key=lambda page: page.title()):
            title = re.sub(r"([\\`*_\[\]<>])", r"\\\1", page.title())
            lines.append(f"- [{title}](<{page.full_url()}>)")
        if self.run_url:
            lines.extend(["", f"Workflow run: {self.run_url}"])
        return "\n".join(lines) + "\n"

    def report(self) -> int | None:
        """Create or update a report only when the category contains pages."""
        category = pywikibot.Category(self.site, CATEGORY_TITLE)
        pages = list(category.members())
        if not pages:
            pywikibot.info("No pages remain in %s", CATEGORY_TITLE)
            return None

        body = self.render_body(category, pages)
        issue = self.find_open_issue()
        if issue is None:
            response = self.session.post(
                self.issues_url,
                json={"title": ISSUE_TITLE, "body": body, "assignees": [ASSIGNEE]},
                timeout=30,
            )
            response.raise_for_status()
            issue_number = int(response.json()["number"])
        else:
            issue_number = issue.number
            if issue.body != body or ASSIGNEE not in issue.assignees:
                response = self.session.patch(
                    f"{self.issues_url}/{issue.number}",
                    json={
                        "body": body,
                        "assignees": sorted({*issue.assignees, ASSIGNEE}),
                    },
                    timeout=30,
                )
                response.raise_for_status()

        pywikibot.info("Reported %d pages in issue #%d", len(pages), issue_number)
        return issue_number


def main() -> None:
    """Report missing Scryfall data with the GitHub Actions token."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default="en", help="Project language code")
    parser.add_argument("--family", default="mtg", help="Pywikibot family key")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    args = parser.parse_args()
    if not args.repository:
        parser.error("Set GITHUB_REPOSITORY or pass --repository")
    token = os.environ.get("GH_TOKEN")
    if not token:
        parser.error("Set GH_TOKEN to a token with permission to write issues")

    run_id = os.environ.get("GITHUB_RUN_ID")
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_url = (
        f"{server_url}/{args.repository}/actions/runs/{run_id}" if run_id else None
    )
    site = get_site(Path(args.config), lang=args.site, family=args.family)
    with requests.Session() as session:
        session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        MissingScryfallDataReporter(site, session, args.repository, run_url).report()


if __name__ == "__main__":
    main()
