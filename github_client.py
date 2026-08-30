import math
import re
import time
from datetime import datetime, timezone
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

TIMEOUTS = (10, 45)


class GitHubError(Exception):
    """Custom exception for GitHub API errors."""
    pass


def parse_pr_ref(pr_ref):
    """
    Parse a PR reference into (owner, repo, number).
    Accepts:
      - Full URL: https://github.com/owner/repo/pull/123 (or with query/anchor/trailing slash)
      - Short form: owner/repo#123
    """
    if not pr_ref or not isinstance(pr_ref, str):
        raise GitHubError("PR reference must be a non-empty string.")

    pr_ref = pr_ref.strip()

    # Short form pattern: owner/repo#123
    short_match = re.match(r"^([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+)#(\d+)$", pr_ref)
    if short_match:
        owner, repo, number = short_match.groups()
        return owner, repo, int(number)

    # URL pattern: https?://github.com/owner/repo/pull/123
    url_match = re.match(r"^https?://github\.com/([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+)/pull/(\d+)(?:/.*)?$", pr_ref, re.IGNORECASE)
    if url_match:
        owner, repo, number = url_match.groups()
        return owner, repo, int(number)

    raise GitHubError(
        f"Invalid PR reference '{pr_ref}'. Expected format: "
        "'https://github.com/owner/repo/pull/123' or 'owner/repo#123'."
    )


def _get_session():
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _handle_response_error(response):
    status_code = response.status_code
    remaining = response.headers.get("X-RateLimit-Remaining")
    reset_header = response.headers.get("X-RateLimit-Reset")

    is_rate_limited = (
        status_code == 429 or
        (status_code == 403 and remaining == "0") or
        (status_code == 403 and "rate limit" in response.text.lower())
    )

    if is_rate_limited:
        reset_str = "soon"
        if reset_header and reset_header.isdigit():
            reset_ts = int(reset_header)
            reset_dt = datetime.fromtimestamp(reset_ts, tz=timezone.utc)
            reset_str = reset_dt.strftime("%Y-%m-%d %H:%M:%S UTC")

        raise GitHubError(
            f"GitHub API rate limit exceeded. Quota resets at roughly {reset_str}. "
            "Note: The unauthenticated API limit is 60 requests/hour per IP address. "
            "Setting a GitHub Personal Access Token increases the limit to 5,000 requests/hour."
        )

    if status_code == 403:
        raise GitHubError(
            "Access forbidden (403). The repository may be private or require authentication."
        )

    if status_code == 404:
        raise GitHubError("Pull request or repository not found (404).")

    raise GitHubError(f"GitHub API returned error HTTP {status_code}: {response.text[:200]}")


def fetch_pr(pr_ref, token=None):
    """
    Fetch PR metadata and changed files list from GitHub API.
    `pr_ref` can be a URL, short form string, or a tuple/list of (owner, repo, number).
    """
    if isinstance(pr_ref, tuple) or isinstance(pr_ref, list):
        owner, repo, number = pr_ref
    else:
        owner, repo, number = parse_pr_ref(pr_ref)

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "PR-Risk-Radar"
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    session = _get_session()

    # 1. Fetch PR Metadata
    pr_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"
    try:
        resp = session.get(pr_url, headers=headers, timeout=TIMEOUTS)
    except requests.exceptions.Timeout:
        raise GitHubError("Connection to GitHub API timed out while fetching PR metadata.")
    except requests.exceptions.RequestException as e:
        raise GitHubError(f"Failed to connect to GitHub API: {str(e)}")

    if not resp.ok:
        _handle_response_error(resp)

    pr_data = resp.json()

    # 2. Fetch Changed Files (Paginated)
    total_changed_files = pr_data.get("changed_files", 0)
    pages_needed = math.ceil(total_changed_files / 100) if total_changed_files > 0 else 1
    max_pages = min(pages_needed, 3)

    files = []
    files_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}/files"

    for page in range(1, max_pages + 1):
        try:
            f_resp = session.get(
                files_url,
                headers=headers,
                params={"per_page": 100, "page": page},
                timeout=TIMEOUTS
            )
        except requests.exceptions.Timeout:
            raise GitHubError(f"Connection to GitHub API timed out while fetching PR files page {page}.")
        except requests.exceptions.RequestException as e:
            raise GitHubError(f"Failed to connect to GitHub API for files page {page}: {str(e)}")

        if not f_resp.ok:
            _handle_response_error(f_resp)

        page_files = f_resp.json()
        if not page_files:
            break
        files.extend(page_files)

    return pr_data, files
