import os
import email
from email import policy
import json
import base64
from PIL import Image
import pytesseract
import google.generativeai as genai
from io import BytesIO

# Configure Gemini
#print("Gemini API Key:", os.getenv("GOOGLE_API_KEY"))
if not os.getenv("GOOGLE_API_KEY"):
    raise Exception("Please set your GOOGLE_API_KEY environment variable.")
genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))

EMAIL_DIR = "downloadable_emails_mixed"
HISTORY_JSON = "gemini_analysis_history.json"
OUTPUT_JSON = "gemini_email_output.json"
CATEGORIES_JSON = "request_type_categories.json"

# Extracts image text using OCR
def extract_text_from_image(image_bytes):
    try:
        image = Image.open(BytesIO(image_bytes))
        text = pytesseract.image_to_string(image)
        return text.strip()
    except Exception as e:
        return f"[Error reading image: {str(e)}]"

def extract_email_content(eml_path):
    with open(eml_path, "rb") as f:
        msg = email.message_from_bytes(f.read(), policy=policy.default)

    email_body = ""
    attachments_content = {}

    if msg.is_multipart():
        for part in msg.walk():
            content_disp = part.get("Content-Disposition", "")
            content_type = part.get_content_type()
            filename = part.get_filename()

            if part.get_content_maintype() == "text" and "attachment" not in content_disp:
                try:
                    email_body += part.get_content()
                except:
                    payload = part.get_payload(decode=True)
                    if payload:
                        email_body += payload.decode("utf-8", errors="replace")

            elif "attachment" in content_disp and filename:
                payload = part.get_payload(decode=True)
                if content_type.startswith("image/"):
                    text = extract_text_from_image(payload)
                    attachments_content[filename] = f"[Extracted Text from Image: {text}]"
                else:
                    try:
                        decoded = payload.decode("utf-8", errors="replace")
                        attachments_content[filename] = decoded
                    except:
                        attachments_content[filename] = f"[Binary attachment: {filename}, could not extract text]"
    else:
        email_body = msg.get_content()

    return email_body, attachments_content

def analyze_email_with_gemini(email_body, attachments_content):
    prompt = (
        "You are an expert in analyzing emails for a Commercial Bank Lending Service. Your task is to process the provided email content and any attachment descriptions, then output a JSON object with the following keys:\n\n"
        "1. 'request_types': a list of objects, each with keys 'type', 'sub_type', 'confidence', and 'reasoning'. These represent the identified request types based on the sender’s intent.\n\n"
        "2. 'extracted_fields': an object with keys such as 'loan_id', 'amount', 'expiration_date', etc., containing the values extracted from the email body and attachments.\n\n"
        "3. 'duplicate_detection': an object with keys 'is_duplicate' (a boolean) and 'reason', indicating if the email appears to be a duplicate request.\n\n"
        "4. If the email contains multiple requests, indicate each, and also specify the primary intent based on the context.\n\n"
        "Below is the email body:\n\n"
        f"{email_body}\n\n"
        "Below are the attachment placeholders:\n\n"
        f"{json.dumps(attachments_content, indent=2)}\n\n"
        "Your response must contain only raw JSON. Do not wrap the output in triple backticks or markdown formatting. Just return a clean JSON object."
    )

    model = genai.GenerativeModel("gemini-1.5-flash", generation_config=genai.types.GenerationConfig(
        temperature=0.0
    ))
    chat = model.start_chat()

    try:
        response = chat.send_message(prompt)
        return response.text
    except Exception as e:
        return f"Error: {e}"

def normalize_request(req):
    return {
        "type": (req.get("type") or "").strip().lower(),
        "sub_type": (req.get("sub_type") or "").strip().lower(),
    }


def normalize_fields(fields):
    return sorted([
        (k.strip().lower(), str(v).replace(",", "").strip().lower())
        for k, v in fields.items()
    ])

# Check for duplicate emails by comparing request types and extracted fields with history
# If request_types and extracted_fields match exactly with any previous email, flag as duplicate
def check_for_duplicate(new_data, history):
    new_requests = sorted(
        [normalize_request(r) for r in new_data.get("request_types", [])],
        key=lambda x: (x["type"], x["sub_type"])
    )
    new_fields = normalize_fields(new_data.get("extracted_fields", {}))

    for previous in history:
        prev_requests = sorted(
            [normalize_request(r) for r in previous.get("request_types", [])],
            key=lambda x: (x["type"], x["sub_type"])
        )
        prev_fields = normalize_fields(previous.get("extracted_fields", {}))

        if new_requests == prev_requests and new_fields == prev_fields:
            return True, previous.get("email_file")
    return False, None

def load_existing_categories():
    if os.path.exists(CATEGORIES_JSON):
        with open(CATEGORIES_JSON, "r") as f:
            return json.load(f)
    return []

def save_categories(categories):
    with open(CATEGORIES_JSON, "w") as f:
        json.dump(categories, f, indent=2)

import difflib

def classify_request_type(new_request_type, existing_categories):
    new_type = new_request_type.get("type", "").strip()
    new_type_normalized = new_type.lower().replace("_", " ")
    best_match = difflib.get_close_matches(new_type_normalized, [c.lower().replace("_", " ") for c in existing_categories], n=1, cutoff=0.6)

    if best_match:
        matched_index = [c.lower().replace("_", " ") for c in existing_categories].index(best_match[0])
        return existing_categories[matched_index]
    else:
        existing_categories.append(new_type)
        save_categories(existing_categories)
        return new_type

CATEGORIES_JSON = "request_type_categories.json"

def load_existing_categories():
    if os.path.exists(CATEGORIES_JSON):
        with open(CATEGORIES_JSON, "r") as f:
            return json.load(f)
    return []

def save_categories(categories):
    with open(CATEGORIES_JSON, "w") as f:
        json.dump(categories, f, indent=2)

def classify_request_type(new_request_type, existing_categories):
    new_type = new_request_type.get("type", "").strip()
    for category in existing_categories:
        if category.strip().lower() == new_type.lower():
            return category
    existing_categories.append(new_type)
    save_categories(existing_categories)
    return new_type

def main():
    results = []

    if os.path.exists(HISTORY_JSON):
        with open(HISTORY_JSON, "r") as f:
            history_data = json.load(f)
    else:
        history_data = []

    existing_categories = load_existing_categories()

    for fname in os.listdir(EMAIL_DIR):
        if fname.endswith(".eml") and fname not in [item["email_file"] for item in history_data]:
            eml_path = os.path.join(EMAIL_DIR, fname)
            print(f"Processing email: {fname}")
            email_body, attachments_content = extract_email_content(eml_path)
            analysis = analyze_email_with_gemini(email_body, attachments_content)

            print(f"--- Analysis for {fname} ---")
            try:
                if analysis.strip().startswith("```json"):
                    analysis = analysis.strip().removeprefix("```json").removesuffix("```").strip()
                elif analysis.strip().startswith("```"):
                    analysis = analysis.strip().removeprefix("```").removesuffix("```").strip()

                parsed = json.loads(analysis)
            except Exception:
                print("⚠️ Could not parse JSON. Dumping raw response:")
                print(analysis)
                parsed = {"parsing_error": analysis.strip()}
                results.append(parsed)
                continue

            is_dup, duplicate_of = check_for_duplicate(parsed, history_data)
            parsed["email_file"] = fname
            parsed.setdefault("duplicate_detection", {})
            parsed["duplicate_detection"]["is_duplicate"] = is_dup
            parsed["duplicate_detection"]["reason"] = f"Matches {duplicate_of}" if is_dup else "No exact match found"

            existing_categories = load_existing_categories()
            for req in parsed.get("request_types", []):
                req["type"] = classify_request_type(req, existing_categories)

            for req in parsed.get("request_types", []):
                req["type"] = classify_request_type(req, existing_categories)

            history_data.append(parsed)
            results.append(parsed)
            print("-" * 80)

    with open(HISTORY_JSON, "w") as f:
        json.dump(history_data, f, indent=2)

    from datetime import datetime

    # Create a timestamped output folder to avoid overwriting previous runs
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    OUTPUT_DIR = f"categorized_outputs_{run_id}"
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # --- (Removed pre-grouped categorized outputs to avoid duplication; only functional grouping will be used) ---

    # Group and export based on functional categories using Gemini
    # Load all previously seen request types for regrouping
    all_request_types = load_existing_categories()

    # Ensure grouping runs even on first run by using just-seen request types
    if not all_request_types:
        all_request_types = list(set(req["type"] for item in results for req in item.get("request_types", [])))
        save_categories(all_request_types)

    regroup_prompt = (
        "You are an expert in categorizing banking service request types. Below is a list of request type labels extracted from emails:"
        + json.dumps(all_request_types, indent=2) +
        "Please group these request types into logical high-level categories based on functionality. "
        "Return a JSON object where each key is a high-level category name and the value is a list of request types that belong to it. "
        "Do not include any commentary, just return a valid JSON."
    )

    model = genai.GenerativeModel("gemini-1.5-flash", generation_config=genai.types.GenerationConfig(temperature=0.0))
    chat = model.start_chat()

    try:
        regroup_response = chat.send_message(regroup_prompt)
        raw_response = regroup_response.text.strip().strip("`")
        if raw_response.startswith("json"):
            raw_response = raw_response[4:].strip()
        regrouped = json.loads(raw_response)
        # Save the high-level grouping returned by Gemini
        grouping_filename = f"functional_grouping_map_{run_id}.json"
        with open(grouping_filename, "w") as fg:
            json.dump(regrouped, fg, indent=2)
        with open("functional_grouping_map.json", "w") as fg:
            json.dump(regrouped, fg, indent=2)

        # Create explanation prompt
        explanation_prompt = (
            "You are an expert in banking service categorization. Given the following JSON mapping of functional groups and their request types, explain briefly (1-2 sentences) why each group includes the request types it does."
            "Return a valid JSON where each key is a group name and the value is a short explanation string."
            + json.dumps(regrouped, indent=2)
        )
        explanation_response = chat.send_message(explanation_prompt)
        explanation_raw = explanation_response.text.strip().strip("`")
        if explanation_raw.startswith("json"):
            explanation_raw = explanation_raw[4:].strip()
        explanation_data = json.loads(explanation_raw)

        explanation_filename = f"functional_grouping_explanation_{run_id}.json"
        with open(explanation_filename, "w") as ef:
            json.dump(explanation_data, ef, indent=2)
    except Exception as e:
        print("Error grouping categories:", e)
        regrouped = {"uncategorized": [c for c in all_request_types]}

    # Create an empty result bucket for each functional category returned by Gemini
    grouped_results = {key: [] for key in regrouped.keys()}

    # Categorize each email based on the functional groupings
    for item in results:
        matched = False
        for group, members in regrouped.items():
            if item.get("request_types") and item["request_types"][0].get("type") in members:
                grouped_results[group].append(item)
                matched = True
                break

        # If not matched, assign to fallback category with low confidence and explanation
        if not matched:
            fallback_group = "Low Confidence - Unmatched"
            grouped_results.setdefault(fallback_group, []).append(item)
            item.setdefault("request_types", [{}])[0]["confidence"] = 0.3
            item["request_types"][0]["reasoning"] = item["request_types"][0].get("reasoning", "") + " | Auto-categorized due to missing match in LLM groupings."
            request_label = item.get("request_types", [{}])[0].get("type", "")
            fallback_group = "Low Confidence - Unmatched"
            grouped_results.setdefault(fallback_group, []).append(item)
            item.setdefault("request_types", [{}])[0]["confidence"] = 0.3
            item["request_types"][0]["reasoning"] = item["request_types"][0].get("reasoning", "") + " | Auto-categorized due to missing match in LLM groupings."

    for group, emails in grouped_results.items():
        file_path = os.path.join(OUTPUT_DIR, f"{group.replace(' ', '_').lower()}.json")
        with open(file_path, "w") as f:
            json.dump(emails, f, indent=2)

    print("✅ Exported grouped results to '" + OUTPUT_DIR + "' folder based on functional categories")

if __name__ == "__main__":
    main()
