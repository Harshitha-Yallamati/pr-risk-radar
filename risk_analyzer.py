import fnmatch
import json
import re
from llm_client import complete_prompt, LLMError

# Sensitive path patterns by category
SENSITIVE_PATTERNS = {
    "auth": [
        "*auth*", "*login*", "*password*", "*jwt*", "*oauth*", "*session*",
        "*permission*", "*role*", "*token*", "*user_service*"
    ],
    "payment": [
        "*stripe*", "*billing*", "*payment*", "*checkout*", "*subscription*",
        "*invoice*", "*paypal*", "*braintree*"
    ],
    "database/migration": [
        "*migration*", "*schema*", "*alembic*", "*db/*", "*models*",
        "*.sql", "*prisma*", "*knex*", "*flyway*"
    ],
    "secrets/config": [
        "*.env*", "*config*", "*settings*", "*secret*", "*key*",
        "*.pem", "*.crt", "*vault*"
    ],
    "infra/ci": [
        "*.github/*", "*docker*", "*Dockerfile*", "*terraform*", "*.tf",
        "*kubernetes*", "*k8s*", "*.yaml", "*.yml", "*helm*", "*ansible*"
    ],
    "security": [
        "*security*", "*crypto*", "*cipher*", "*policy*", "*cors*",
        "*sanitize*", "*csp*"
    ]
}

TEST_PATTERNS = [
    "*test*", "*spec*", "*tests/*", "*specs/*", "__tests__/*"
]


def is_match(filepath, patterns):
    filepath_lower = filepath.lower()
    for pattern in patterns:
        if fnmatch.fnmatch(filepath_lower, pattern.lower()):
            return True
    return False


def classify_files(files):
    """
    Classify changed files against sensitive path patterns and test patterns.
    """
    categorized_files = {category: [] for category in SENSITIVE_PATTERNS}
    test_files_count = 0
    non_test_files_count = 0

    for file_obj in files:
        filename = file_obj.get("filename", "")
        status = file_obj.get("status", "modified")
        additions = file_obj.get("additions", 0)
        deletions = file_obj.get("deletions", 0)

        # Test classification
        if is_match(filename, TEST_PATTERNS):
            test_files_count += 1
        else:
            non_test_files_count += 1

        # Category classification
        for category, patterns in SENSITIVE_PATTERNS.items():
            if is_match(filename, patterns):
                categorized_files[category].append({
                    "filename": filename,
                    "status": status,
                    "additions": additions,
                    "deletions": deletions
                })

    return categorized_files, test_files_count, non_test_files_count


def build_signals_summary(pr_data, files):
    """
    Build signals summary from files list and PR metadata.
    """
    categorized_files, test_files_count, non_test_files_count = classify_files(files)

    total_files = len(files)
    total_additions = sum(f.get("additions", 0) for f in files)
    total_deletions = sum(f.get("deletions", 0) for f in files)

    return {
        "total_files": total_files,
        "total_additions": total_additions,
        "total_deletions": total_deletions,
        "test_files_count": test_files_count,
        "non_test_files_count": non_test_files_count,
        "sensitive_matches": categorized_files
    }


def build_diff_excerpt(files, file_max_chars=1500, total_max_chars=18000):
    """
    Build diff excerpt for the model, truncating each file's patch to ~1500 chars
    and total excerpt to ~18000 chars.
    """
    excerpt_parts = []
    current_length = 0
    was_truncated = False

    for file_obj in files:
        filename = file_obj.get("filename", "unknown")
        patch = file_obj.get("patch", "")

        header = f"--- File: {filename} (status: {file_obj.get('status', 'modified')}) ---\n"

        if not patch:
            content = header + "[No patch available or binary file]\n\n"
        else:
            if len(patch) > file_max_chars:
                patch = patch[:file_max_chars] + f"\n... [File patch truncated at {file_max_chars} chars] ...\n"

            content = header + patch + "\n\n"

        if current_length + len(content) > total_max_chars:
            remaining_chars = total_max_chars - current_length
            if remaining_chars > 200:
                content = content[:remaining_chars] + "\n... [Total diff excerpt truncated due to length limits] ...\n"
                excerpt_parts.append(content)
            else:
                excerpt_parts.append("\n... [Total diff excerpt truncated due to length limits] ...\n")
            was_truncated = True
            break

        excerpt_parts.append(content)
        current_length += len(content)

    return "".join(excerpt_parts), was_truncated


def parse_json_response(raw_text):
    """
    Parse the LLM response defensively. Strip markdown fences and fall back
    to extracting outermost JSON object.
    """
    if not raw_text:
        return None

    cleaned = raw_text.strip()

    # Strip markdown code fences if present (e.g. ```json ... ```)
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Fall back to finding outermost JSON object `{ ... }`
    json_match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass

    return None


def analyze_pr_risk(pr_data, files):
    """
    Runs pattern-based heuristics, builds diff excerpt, prompts LLM,
    and returns structured risk assessment attached with signals.
    """
    signals = build_signals_summary(pr_data, files)
    diff_excerpt, was_truncated = build_diff_excerpt(files)

    pr_title = pr_data.get("title", "")
    pr_body = pr_data.get("body", "") or "No description provided."

    # Build prompt
    prompt = f"""You are a senior staff software engineer performing pre-review risk triage on a GitHub pull request.
Your job is to identify hidden risks, under-stated PR descriptions, security concerns, or architectural hazards before human code review.

PR Title: {pr_title}
PR Description:
{pr_body}

Heuristic Signals Detected:
- Total Files Changed: {signals['total_files']} (+{signals['total_additions']}, -{signals['total_deletions']})
- Test vs Non-Test Files: {signals['test_files_count']} test files, {signals['non_test_files_count']} non-test files
- Sensitive Categories Touched:
"""

    category_summary = []
    for category, matched_files in signals["sensitive_matches"].items():
        if matched_files:
            file_names = [f["filename"] for f in matched_files]
            category_summary.append(f"  * {category}: {', '.join(file_names)}")

    if category_summary:
        prompt += "\n".join(category_summary) + "\n"
    else:
        prompt += "  * None\n"

    if was_truncated:
        prompt += "\nNOTE: The diff excerpt below was truncated due to length limits. Please be conservative in your risk assessment.\n"

    prompt += f"\nDiff Excerpt:\n{diff_excerpt}\n"

    prompt += """
Respond ONLY with a valid JSON object (no markdown formatting, no explanatory text outside the JSON) containing exactly these keys:
- "risk_level": String ("Low", "Medium", or "High")
- "risk_score": Integer (0 to 100)
- "summary": String summarizing the PR risk and if the title/description understates the actual diff changes
- "concerns": Array of strings (specific risk items, citing actual filenames from the diff)
- "reviewer_focus": Array of strings (specific areas or files reviewer should pay close attention to)
- "categories_touched": Array of strings (the sensitive categories impacted)

Be specific and cite actual filenames from the diff rather than giving generic advice. Be conservative if the diff was truncated.
"""

    parsed_result = None
    try:
        raw_response = complete_prompt(prompt, max_tokens=4000)
        parsed_result = parse_json_response(raw_response)
    except LLMError as e:
        parsed_result = {
            "risk_level": "Unknown",
            "risk_score": 0,
            "summary": f"Failed to perform LLM analysis: {str(e)}",
            "concerns": ["LLM API call failed or encountered error."],
            "reviewer_focus": ["Review diff manually."],
            "categories_touched": [cat for cat, f in signals["sensitive_matches"].items() if f]
        }

    if not parsed_result or not isinstance(parsed_result, dict):
        parsed_result = {
            "risk_level": "Medium",
            "risk_score": 50,
            "summary": "The LLM response could not be parsed into valid JSON structure.",
            "concerns": ["LLM returned malformed output."],
            "reviewer_focus": ["Perform full manual code review."],
            "categories_touched": [cat for cat, f in signals["sensitive_matches"].items() if f]
        }

    parsed_result["signals"] = signals
    return parsed_result
