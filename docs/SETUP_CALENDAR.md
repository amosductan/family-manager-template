# Connecting Google Calendar

The app reads every calendar on your Google account, merges duplicates across them, answers
invitations from people you trust, and creates the day-off and pickup events you ask for.
Google Calendar only accepts OAuth, so you create a small OAuth client in your own Google Cloud
project. It takes about five minutes.

## Create the client

1. Open [console.cloud.google.com](https://console.cloud.google.com) signed in as the account
   whose calendar the app should use.
2. Create a project, e.g. `family-manager`.
3. **APIs & Services > Library**: enable **Google Calendar API**.
4. **Google Auth Platform > Branding**: app name `Family Manager`, your email for support and
   developer contact.
5. **Audience**: user type **External**, and add your own address under **Test users**.
6. **Clients > Create client**: type **Desktop app**. Download the JSON.
7. Save it as `data/gcal_client_secret.json`. `data/` is never committed.

## Authorize

```bash
python gcal.py --authorize
```

A browser opens. Google warns the app is unverified, which is expected for an app you built:
**Advanced > Go to Family Manager (unsafe) > Continue**. The token lands in `data/gcal_token.json`.

Check it:

```bash
python gcal.py --token-health    # expect "token: OK"
python gcal.py --dry-run         # lists your calendars and what would be read
```

**The 7-day limit.** While the app is in Testing, Google expires the refresh token after 7 days,
so you'd re-run `--authorize` weekly. Publishing the app (Audience > Publish app) removes the
limit. Publishing asks for a home page and a privacy policy URL on a domain you control; the app
serves `/privacy`.

## Authorizing a machine with no browser

If the app runs on an always-on machine you reach over SSH:

```bash
# on your laptop: forward the redirect port (use 127.0.0.1 on both ends; "localhost" can
# resolve to ::1 and the redirect fails)
ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 you@always-on-machine
# on the always-on machine:
python gcal.py --authorize --authorize-port 8765 --no-browser
```

Open the printed URL in your laptop's browser, approve, and the redirect rides the tunnel back.

## What the invitation rules do

- **Accept** an invitation from a trusted organizer (`trusted_organizers` in
  `data/household.json`) when it doesn't collide with anything you've accepted.
- **Flag** it when it collides. The rules never decline; declining says something about your
  intent that no rule should say for you.
- **Skip** anything from anyone else.
- Responses go out without notification emails.
