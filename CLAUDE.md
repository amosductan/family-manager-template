# Family Manager: instructions for Claude Code

This file is read by Claude Code when someone opens this folder. Most people open it to say
**"set up my family."** Follow the setup script below. Do the work yourself; ask the person
only for what only they know or can do (their family's details, creating an app password,
approving calendar access).

## Setup script ("set up my family")

1. **Check the machine.** `python --version` must be 3.11+. Run `pip install -r requirements.txt`.
   Run `python family.py --self-test`. It must print `0 failure(s)`.
2. **Interview the household.** Ask, in one message, for:
   - each parent's name and email, and which inbox the app should read (usually one parent's
     Gmail; the other parent's forwards can be a source);
   - each kid's name, birthday (month and day), grade, school name and the short name people
     use, and any nickname that shows up in calendar titles;
   - who else covers days the kids are out (camp, grandparents, a sitter);
   - the email domains the schools, camps and activities write from, if they know them.
3. **Clear the demo, then write `data/household.json`.** If `python demo_seed.py` was run, run
   `python demo_seed.py --clear` first, or the example kids stay on the board. Then write
   `data/household.json` in the shape of `config/household.example.json`. Every
   school or activity domain becomes a `sources` entry. A partner who forwards school mail is a
   source with `keywords`, so only school-looking forwards count. Run `python family.py`; it
   must print the household back without an error.
4. **Gmail.** The person creates an app password (Google Account > Security > 2-Step
   Verification > App passwords; 2-Step Verification must be on). Put it in `.env` as
   `GMAIL_APP_PASSWORD=...` (copy `.env.example`). Never print it back.
5. **First mail check.** `python ingest.py --days 60`. Then start the app (`python app.py`) and
   open `/mail/sources`: the discovery pass suggests senders that look like school mail. Go
   through them with the person and approve the real ones.
6. **Calendar (optional).** Follow [docs/SETUP_CALENDAR.md](docs/SETUP_CALENDAR.md). The
   person has to click through Google's console and consent screen; you can do everything else.
7. **Schedule the nightly run** on a machine that stays on: the mail check, then
   `python gcal.py --rsvp` if the calendar is set up, then `python job_report.py --alert`.
   - macOS/Linux: a cron line, e.g. `0 16 * * * cd /path/to/family-manager && python ingest.py && python gcal.py --rsvp; python job_report.py --alert`
   - Windows: a Task Scheduler task whose **working directory is this folder**, set to run on
     battery too.
8. **Phones (optional).** The app listens on 127.0.0.1. To reach it from phones, put the
   machine on [Tailscale](https://tailscale.com) and `tailscale serve` the port. With
   `AUTH_MODE=dev` the tailnet is the only lock, so only add people you'd hand your house key.
9. **Show them it works.** Open the home page and `/days-off`, and ask one question on `/ask`.

## Rules for working in this repo

- A family's names, schools and addresses live ONLY in `data/household.json`. Code reads them
  from `family.py`. Never write a real name into code, templates or tests; tests use the example
  family via `family.use_example()`.
- `data/` and `.env` never get committed.
- Run the module self-tests after changing a module (`python <module>.py --self-test`) and
  `python test_household.py`.
- After any template or CSS change, run `python check_mobile.py http://127.0.0.1:5088`. The
  phone is the main screen, and a page can look fine on a laptop while scrolling sideways on a phone.
- Flask caches templates when debug is off. Restart the app before trusting a check.
- Model calls go through `claude_headless.py` only. It strips `ANTHROPIC_API_KEY` so a key in
  the environment is never billed, and it logs cost to `data/model_costs.jsonl`.
