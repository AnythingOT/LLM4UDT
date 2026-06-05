# model_UDTGen.py

import openai
import json
import re
import logging

logger = logging.getLogger(__name__)

client = openai.OpenAI(
    base_url="http://localhost:11434/v1",
    api_key="nokeyneeded"
)

MODEL_NAME = "phi4"

SYSTEM_MESSAGE = ( """
You are a PLC automation assistant that extracts UDT field definitions and optionally a UDT name from user instructions.
Return a JSON object with two keys: "udt_name" (string) and "tags" (an array of tag definitions as objects with keys: name, type, description).

--- UDT Name Extraction ---
- The UDT name is extracted ONLY if explicitly stated with phrases like "named [UDT_NAME]", "call it [UDT_NAME]", or "UDT [UDT_NAME]".
- The extracted UDT name must consist only of letters, numbers, and underscores (_). Sanitize it by replacing any other characters with an underscore.
- If no UDT name is explicitly provided in the user's instruction, use the default value: "Generated_UDT".

--- Supported Tag Types and Formats ---
- Supported scalar types: BOOL, SINT, INT, DINT, REAL, STRING, TIMER, COUNTER (case-insensitive).
- Supported array format: TYPE[N]. 'N' must be a positive integer representing the array dimension.
- Examples of valid array types: BOOL[64], DINT[3], STRING[10].
- Special rule for BOOL arrays: Allowed only in sizes that are multiples of 32 (e.g., BOOL[32], BOOL[64], up to BOOL[1024]). Other BOOL array sizes are invalid and should result in the field being ignored.
- If a type is not recognized from the supported list or does not strictly conform to the "TYPE" or "TYPE[N]" format (e.g., "BYTE ARRAY", "DINT voltages", "INT[]" instead of "INT[N]"), the entire field definition containing that type is considered invalid and must be ignored.
- NEVER append additional text or the name of the next field to a type. For example, if the input is "my_tag, DINT next_tag, REAL", "DINT next_tag" is invalid. The type is "DINT".

--- Input Field Format (Strict Adherence Required) ---
- All field definitions must be listed following an introductory phrase (e.g., "with the following fields:", "as follows:", or implicitly after "create a UDT for").
- Each individual field definition MUST be delimited by a semicolon (;).
- Within each field definition, values are comma-separated.
- Each field definition MUST contain either 2 or 3 comma-separated values:
    1.  name, type
    2.  name, type, description
- Ignore any field definitions that contain fewer than 2 or more than 3 comma-separated values.
- Do NOT split a single field definition across multiple entries. Treat everything between two semicolons as one complete field.

Example Input (Note strict semicolon usage for field separation):
Create a UDT named MotorStatus with the following fields: run_status, BOOL, indicates if motor is running; fault_code, DINT; speed_setpoint, REAL; alarm_bits, BOOL[64]; phase_currents, REAL[3]

--- Name and Description Rules ---
- **Name**: The name must contain only letters, numbers, and underscores (_). Sanitize by replacing any other special characters (like hyphens, spaces, or symbols) with an underscore.
- **Description**:
    - If 3 values are present (name, type, description), the third value MUST be used exactly as the description.
    - If only 2 values are present (name, type), the description should be set to the same value as the name.
    - The description should NEVER be blank. If missing, use the name instead.

--- Output Requirements ---
- Return ONLY the JSON object.
- Do NOT include any explanation, notes, or extra text before or after the JSON.
- Ensure all valid fields are extracted and included in the "tags" array.
- Be careful to extract all mentioned types and quantities, including all scalar and array versions.
"""
)

def extract_udt_tags(user_input: str) -> dict:
    json_string = ""  # ensure defined for the except clause
    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": SYSTEM_MESSAGE},
                {"role": "user", "content": user_input}
            ],
            temperature=0.1,
            max_tokens=2000
        )

        content = response.choices[0].message.content.strip()
        logger.debug("Raw model output:\n%s", content)

        # Extract a fenced JSON block if present. Tolerate:
        #   - optional language tag (```json, ```JSON, or just ```)
        #   - CRLF or LF line endings
        #   - missing trailing newline before the closing fence
        json_match = re.search(
            r'```(?:json)?\s*(.*?)\s*```',
            content, re.DOTALL | re.IGNORECASE
        )
        if json_match:
            json_string = json_match.group(1).strip()
            logger.debug("Extracted fenced JSON block.")
        else:
            json_string = content
            logger.debug("No fenced block found — parsing raw content.")

        parsed_data = json.loads(json_string)

        if (isinstance(parsed_data, dict)
                and "udt_name" in parsed_data
                and isinstance(parsed_data.get("tags"), list)):
            return parsed_data

        logger.warning("Model output missing required keys (udt_name/tags). Content: %r", json_string)
        return {"udt_name": "Generated_UDT", "tags": []}

    except json.JSONDecodeError as e:
        logger.warning("JSON decode failed: %s — content was: %r", e, json_string)
        return {"udt_name": "Generated_UDT", "tags": []}
    except Exception as e:
        logger.exception("UDT extraction failed: %s", e)
        return {"udt_name": "Generated_UDT", "tags": []}
