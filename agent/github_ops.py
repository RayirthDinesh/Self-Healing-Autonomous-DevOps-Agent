"""GitHub API operations: opening a pull request for the auto-fix branch."""

import logging

import requests

logger = logging.getLogger("sre-agent-webhook")


def create_pull_request(token: str, repo: str, head: str, base: str,
                        title: str, body: str) -> str:
    """Open a pull request on GitHub and return its URL."""
    response = requests.post(
        f"https://api.github.com/repos/{repo}/pulls",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        },
        json={
            "title": title,
            "body": body,
            "head": head,
            "base": base,
        },
        timeout=30,
    )
    response.raise_for_status()
    pr_url = response.json()["html_url"]
    logger.info("PR opened: %s", pr_url)
    return pr_url
