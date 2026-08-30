import html
import logging
import os
import re
import time
from dotenv import load_dotenv
import markupsafe
from flask import Flask, flash, redirect, render_template, request, url_for

from github_client import GitHubError, fetch_pr, parse_pr_ref
from llm_client import LLMError, PROVIDERS, resolve_provider
from risk_analyzer import analyze_pr_risk

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("pr_risk_radar")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-pr-risk-radar")


def map_llm_error_message(error_msg):
    msg_lower = error_msg.lower()
    if "401" in msg_lower or "invalid_api_key" in msg_lower or "key rejected" in msg_lower or "unauthorized" in msg_lower:
        return "LLM API key was rejected or is invalid. Please check your API key configuration."
    if "429" in msg_lower or "rate limit" in msg_lower or "quota" in msg_lower or "out of credits" in msg_lower or "insufficient_quota" in msg_lower:
        return "LLM API rate limit or credit limit reached. Please try again later or check your LLM provider quota/credits."
    if "404" in msg_lower or "model_not_found" in msg_lower:
        return "The requested LLM model was not found or is not supported by the provider."
    return f"LLM Service Error: {error_msg}"


@app.template_filter("backtick_code")
@app.template_filter("inline_code")
def backtick_code_filter(s):
    """
    Template filter converting markdown backtick spans (`code`) into HTML <code> elements.
    Text is HTML-escaped first to prevent markup injection.
    """
    if not s or not isinstance(s, str):
        return ""

    # Escape HTML characters first
    escaped = html.escape(s)

    # Convert `code` to <code>code</code>
    pattern = r"`([^`]+)`"
    replaced = re.sub(pattern, r"<code>\1</code>", escaped)

    return markupsafe.Markup(replaced)


@app.route("/", methods=["GET"])
def index():
    provider_info = None
    warning_msg = None

    try:
        provider, _ = resolve_provider()
        model = os.getenv("LLM_MODEL") or provider["default_model"]
        provider_info = {
            "name": provider["id"],
            "model": model,
            "env_var": provider["env_var"]
        }
    except LLMError as e:
        warning_msg = str(e)

    return render_template(
        "index.html",
        provider_info=provider_info,
        warning_msg=warning_msg
    )


@app.route("/analyze", methods=["POST"])
def analyze():
    pr_ref = request.form.get("pr_url", "").strip()
    token = request.form.get("github_token", "").strip() or os.getenv("GITHUB_TOKEN")

    if not pr_ref:
        flash("Please provide a GitHub pull request URL or short form.", "danger")
        return redirect(url_for("index"))

    # 1. Fetch GitHub PR Data
    gh_start = time.time()
    try:
        pr_data, files = fetch_pr(pr_ref, token=token)
        gh_elapsed = time.time() - gh_start
        logger.info(f"GitHub PR fetch completed in {gh_elapsed:.2f}s for PR '{pr_ref}'")
    except GitHubError as e:
        flash(f"GitHub Error: {str(e)}", "danger")
        return redirect(url_for("index"))
    except Exception as e:
        logger.exception("Unexpected error during GitHub fetch")
        flash(f"Unexpected error fetching PR: {str(e)}", "danger")
        return redirect(url_for("index"))

    # 2. Risk Analysis & LLM Call
    llm_start = time.time()
    try:
        report = analyze_pr_risk(pr_data, files)
        llm_elapsed = time.time() - llm_start
        logger.info(f"LLM risk analysis completed in {llm_elapsed:.2f}s for PR '{pr_ref}'")
    except LLMError as e:
        mapped_msg = map_llm_error_message(str(e))
        flash(mapped_msg, "danger")
        return redirect(url_for("index"))
    except Exception as e:
        logger.exception("Unexpected error during risk analysis")
        flash(f"Analysis failed: {str(e)}", "danger")
        return redirect(url_for("index"))

    return render_template(
        "result.html",
        pr_data=pr_data,
        report=report,
        gh_elapsed=round(gh_elapsed, 2),
        llm_elapsed=round(llm_elapsed, 2)
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "true").lower() in ("true", "1", "t")
    app.run(host="0.0.0.0", port=port, debug=debug)
