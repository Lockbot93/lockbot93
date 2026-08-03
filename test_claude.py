import os

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

# The .env file defines CLAUDE_API_KEY. This script used to look for
# ANTHROPIC_API_KEY, which is not set anywhere, so it raised on every run
# and never once reached the API. Accept either name.
api_key = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY")

if not api_key:
    raise RuntimeError(
        "No Claude API key found. Set CLAUDE_API_KEY in the .env file."
    )

client = Anthropic(api_key=api_key)

print("Connecting to Claude...")

try:
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=100,
        messages=[
            {
                "role": "user",
                "content": (
                    "Reply with exactly this sentence and nothing else: "
                    "Claude connection successful."
                ),
            }
        ],
    )

    print(response.content[0].text)

except Exception as error:
    print("Claude connection failed.")
    print(error)