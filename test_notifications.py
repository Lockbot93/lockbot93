from dotenv import load_dotenv
import os

load_dotenv()

user_key = os.getenv("PUSHOVER_USER_KEY")
app_token = os.getenv("PUSHOVER_APP_TOKEN")

print("PUSHOVER_USER_KEY loaded:", bool(user_key))
print("PUSHOVER_APP_TOKEN loaded:", bool(app_token))

if user_key and app_token:
    print("Status: Pushover credentials loaded successfully.")
else:
    print("Status: One or more Pushover credentials are missing.")