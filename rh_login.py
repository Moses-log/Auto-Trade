"""rh_login.py — Regenerate a fresh Robinhood session pickle locally.

Run this in your OWN terminal (PowerShell or Command Prompt), not through an
automated tool, because Robinhood will prompt you for an SMS/MFA code that you
must type in:

    py rh_login.py

On success it writes ~/.tokens/robinhood.pickle. Then run:

    py upload_pickle.py

to push that fresh session up to the Render server. Watch Discord for the
"📦 ROBINHOOD SESSION RESTORED — Session activated via pickle upload" message.
"""
import getpass
import os

import robin_stocks.robinhood as r

PICKLE_PATH = os.path.expanduser("~/.tokens/robinhood.pickle")


def main() -> None:
    print("Robinhood login — this creates a fresh session pickle.\n")
    username = input("Robinhood email/username: ").strip()
    password = getpass.getpass("Robinhood password: ")

    # store_session=True writes ~/.tokens/robinhood.pickle. If your account has
    # 2FA, robin_stocks will prompt you for the SMS/MFA code right here.
    r.login(username=username, password=password, store_session=True)

    # Verify the session actually works for data calls (same check the server does).
    profile = r.load_account_profile()
    if not profile:
        print("\n❌ Login did not return an account profile — session not valid. Try again.")
        return

    if os.path.exists(PICKLE_PATH):
        print(f"\n✅ Fresh session pickle written to: {PICKLE_PATH}")
        print("   Account profile loaded OK — session is valid for data calls.")
        print("\nNext: run  py upload_pickle.py  to push it to the server.")
    else:
        print(f"\n⚠️ Login succeeded but no pickle found at {PICKLE_PATH}.")
        print("   Check your robin_stocks version / tokens directory.")


if __name__ == "__main__":
    main()
