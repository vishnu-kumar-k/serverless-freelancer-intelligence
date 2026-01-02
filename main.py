import os
import json
import urllib.request
import boto3
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

# =======================
# AWS RESOURCES
# =======================
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table("freelancer_jobs")

bedrock = boto3.client(
    "bedrock-runtime",
    region_name="us-east-1"
)

# =======================
# ENV CONFIG
# =======================
KEYWORDS = [k.strip().lower() for k in os.environ["JOB_KEYWORDS"].split(",")]
MIN_BUDGET = int(os.environ["MIN_BUDGET"])
SCORE_THRESHOLD = int(os.environ["AI_SCORE_THRESHOLD"])
PROFILE = os.environ["YOUR_PROFILE_SUMMARY"]

REQUIRE_PAYMENT_VERIFIED = os.environ.get(
    "REQUIRE_PAYMENT_VERIFIED", "true"
).lower() == "true"

MAX_PAGES = 5
PAGE_LIMIT = 50

IST = ZoneInfo("Asia/Kolkata")

# =======================
# TELEGRAM
# =======================
def send_telegram_message(message: str):
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
    payload = {
        "chat_id": os.environ["TELEGRAM_CHAT_ID"],
        "text": message,
        "disable_web_page_preview": True
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(req).read()

# =======================
# FREELANCER API (PAGINATED)
# =======================
def fetch_all_projects():
    access_token = os.environ["FL_ACCESS_TOKEN"]
    all_projects = []

    for page in range(MAX_PAGES):
        params = {
            "query": " ".join(KEYWORDS),
            "limit": PAGE_LIMIT,
            "offset": page * PAGE_LIMIT
        }

        url = (
            "https://www.freelancer.com/api/projects/0.1/projects/active?"
            + urlencode(params)
        )

        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json"
            }
        )

        response = json.loads(urllib.request.urlopen(req).read())
        projects = response.get("result", {}).get("projects", [])

        if not projects:
            break

        all_projects.extend(projects)

    return all_projects

# =======================
# FILTERS
# =======================
def passes_filters(project: dict) -> bool:
    title = (project.get("title") or "").lower()
    description = (project.get("description") or "").lower()

    # Keyword filter
    if not any(k in title or k in description for k in KEYWORDS):
        return False

    # Budget filter
    budget = project.get("budget") or {}
    if budget.get("minimum") is None or budget["minimum"] < MIN_BUDGET:
        return False

    # Payment verification filter (ENV controlled)
    owner = project.get("owner") or {}
    payment_verified = owner.get("payment_verified", False)

    if REQUIRE_PAYMENT_VERIFIED and not payment_verified:
        return False

    return True

# =======================
# AI HELPERS
# =======================
def invoke_bedrock(model_id: str, prompt: str, max_tokens=600) -> str:
    body = {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens
    }

    response = bedrock.invoke_model(
        modelId=model_id,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json"
    )

    result = json.loads(response["body"].read())
    return result["content"][0]["text"]

def score_job(project: dict) -> dict:
    prompt = f"""
You are evaluating a freelance job for relevance.

My skills:
- React, Node.js, backend APIs
- AWS, DevOps, Linux, automation
- Python scripting
- AI integration (not data science)

Job:
Title: {project.get('title')}
Description: {project.get('description')}
Budget: {project.get('budget')}

Return JSON only:
{{"score": number, "reason": "short explanation"}}
"""
    text = invoke_bedrock(
        "anthropic.claude-3-haiku-20240307-v1:0",
        prompt,
        max_tokens=300
    )
    return json.loads(text)

def draft_proposal(project: dict) -> str:
    prompt = f"""
Write a concise, professional freelance proposal.

Job:
Title: {project.get('title')}
Description: {project.get('description')}

My background:
{PROFILE}

Rules:
- 5–7 sentences
- Mention the client’s problem
- Explain approach
- End with a simple next step
"""
    return invoke_bedrock(
        "anthropic.claude-3-sonnet-20240229-v1:0",
        prompt,
        max_tokens=700
    )

# =======================
# TIME FORMATTER
# =======================
def format_posted_time(project: dict) -> str:
    ts = project.get("submitdate")
    if not ts:
        return "Unknown"

    dt = datetime.fromtimestamp(ts, IST)
    return dt.strftime("%d %b %Y, %I:%M %p IST")

# =======================
# LAMBDA HANDLER
# =======================
def lambda_handler(event, context):
    projects = fetch_all_projects()

    total_fetched = len(projects)
    passed_filters = 0
    shortlisted = 0

    for project in projects:
        project_id = str(project["id"])

        if not passes_filters(project):
            continue
        passed_filters += 1

        if "Item" in table.get_item(Key={"project_id": project_id}):
            continue

        score_data = score_job(project)
        score = score_data["score"]

        if score < SCORE_THRESHOLD:
            continue

        shortlisted += 1
        proposal = draft_proposal(project)
        posted_time = format_posted_time(project)

        owner = project.get("owner") or {}
        payment_verified = owner.get("payment_verified", False)

        table.put_item(
            Item={
                "project_id": project_id,
                "title": project["title"],
                "status": "shortlisted",
                "ai_score": score,
                "first_seen_at": datetime.utcnow().isoformat(),
                "source": "freelancer"
            }
        )

        send_telegram_message(
            f"⭐ High-Match Job ({score}/100)\n\n"
            f"📌 {project['title']}\n"
            f"🕒 Posted: {posted_time}\n"
            f"💳 Payment Verified: {'Yes' if payment_verified else 'No'}\n\n"
            f"📝 Proposal Draft:\n{proposal}\n\n"
            f"🔗 https://www.freelancer.com/projects/{project_id}"
        )

    if shortlisted == 0:
        now = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
        send_telegram_message(
            f"📊 Run Summary ({now})\n\n"
            f"Fetched: {total_fetched}\n"
            f"Passed filters: {passed_filters}\n"
            f"Shortlisted: 0\n\n"
            f"No high-match jobs found this run."
        )

    return {
        "statusCode": 200,
        "body": {
            "fetched": total_fetched,
            "passed_filters": passed_filters,
            "shortlisted": shortlisted
        }
    }
