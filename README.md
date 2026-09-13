# BuildCommand AI 8.8.0 — Daily Command Briefing

Built from the working 8.7.0 Blueprint-to-Field release. This build brings
project priorities, shared-work attention, issued scopes and the
superintendent's notes into a reviewed daily briefing with saved history.
It has not been deployed to your Render service.

## Install on staging

1. Extract `BuildCommand_AI_8_8_0.zip`.
2. Put all four application files in the same repository folder:
   - Replace `full_app.py`.
   - Replace `blueprint_field.py` with this release's copy.
   - Add the new `daily_command.py`.
   - Keep `owner_console.py` alongside them; the working companion is included.
3. Commit and deploy to staging. If your Dockerfile copies individual files,
   include the new `daily_command.py` in the image as well.
4. Keep the current start command, database, environment variables and working
   `owner_app.py`. The owner console is a companion, not an ASGI entry point.
5. Open `/health/daily-command-8-8-0` as the owner/admin or signed out. Expect
   **8.8.0**, **status: ok**, **12/12** checks.

The app creates two additional briefing tables and their indexes. It does not
reset data or activate subscriptions. There are no new production dependencies.
Keep your previous application files before replacing them. The older feature
health URLs keep their feature versions, including 8.7.0 for scope publishing
and 8.6.4 for the owner companion.

The `verification` folder is evidence and reproduction material; it does not
need to be deployed.

## First staging workflow

1. Sign in as a company administrator, an appointed superintendent, or an
   appointed project manager. Select the intended project.
2. Open **Command → Review today's brief**. Review team attention, project
   priorities, the latest completed Blueprint analysis reference and issued
   trade scopes.
3. Enter **Superintendent's notes**: responsible people, follow-up times,
   inspection plans or today's field direction.
4. Select **Preview daily briefing**. Review the displayed copy and any
   unavailable or abbreviated sections.
5. Confirm the review checkbox and select **Save reviewed briefing**.
6. Open **Print / Save PDF** for the print view. Use your browser's Print
   command to print or save a PDF. **Download text** provides a portable copy.
7. Return to **Command → Saved briefs** and open the saved briefing again.
8. Have an assigned subcontractor send a progress update through **My shared
   work**. Confirm that Command and a newly opened briefing show the update,
   while the saved briefing retains its original contents.
9. Check that a subcontractor and a superintendent who is not assigned to this
   project cannot view, print or download its saved briefing.

If the displayed records change while you are reviewing a draft, reopen the
current briefing and review it again. Previews expire after 15 minutes and are
tied to the signed-in person and browser session. Saving twice with the same
preview does not create a second copy.

## What is included

- **Daily Command entry:** a clear briefing entry point, saved history, scope
  publishing and the existing Blueprint Brain, photo, Morning Brief and daily
  report shortcuts.
- **Team attention:** blocked, overdue, awaiting-review and ready-for-review
  counts, with subcontractor updates. Earlier unread updates remain visible
  when a later update has already been reviewed.
- **Project priorities:** the existing connected project intelligence, grouped
  into morning, midday and closeout. Source/responsibility references are shown
  where the engine provides them.
- **Issued scopes:** current publications, named subcontractors, status and
  due dates, along with the latest completed Blueprint analysis reference.
- **Reviewed notes:** internal field direction stored in the saved briefing.
- **Fixed saved copies:** history, text download and a separate print view.
  Later project updates do not rewrite a reviewed briefing.
- **Preserved access controls:** only existing authorized managers on the
  relevant company project can use these screens. Subcontractor, company and
  subscription restrictions still apply on reads, writes and downloads.

The briefing uses existing records and the existing project-priority engine.
It does not run a new external AI request, issue work, close a blocker, mark an
update reviewed, change a schedule, or send messages. AI Morning Brief remains
available as a separate existing tool. Earlier analysis briefs remain under
Project analysis; no historical brief records are deleted or converted.

Counts cover all current shared work and may overlap. Due dates use the UTC
calendar. For readability, a briefing lists up to 30 attention items and the
20 newest current scope publications. If more exist, the briefing and download
state the displayed and total counts. Open Command or Trade sharing for the
remaining items. The project-priority engine contributes up to three items per
daily block. Unavailable priority results are labeled; they are not presented
as an all-clear result.

## Code organization

`daily_command.py` owns daily briefing collection, review tokens, immutable
saved copies, history, print/text output and the Command panel.
`blueprint_field.py` continues to own reviewed trade-scope publishing. The old
Command panel was removed from that module and moved into the new service.
`full_app.py` contains the explicit integration and release registration.
`owner_console.py` retains the working companion implementation.

The new tables are `bc_daily_brief_reviews` and `bc_daily_command_briefs`.
Saving rechecks live manager access and the displayed source data, writes the
briefing and audit event, and consumes the preview in one transaction. Source
records and previously saved briefings have no update operation in this module.
Expired, unused review drafts are cleaned up as new previews are prepared.

## Verification completed locally

- **467 regression tests passed**, including **54 new daily briefing checks**.
- **145 full-app checks passed**, using real application sessions, actual
  handlers and access middleware with a disposable local database.
- **12/12 installation checks passed** for 8.8.0.
- The complete application loaded with **1,449 routes**.
- Rendered HTML was inspected for expected headings, controls and form nesting.

The tests cover review/save, changed project records, changed roles and
assignments, different companies, session binding, expiry, replay, transaction
rollback, HTML escaping, foreign-origin POSTs, unavailable priority data,
abbreviated lists, earlier unread updates, preserved saved copies, print/text
access and subscription gates. The 8.7 scope handoff and earlier invitation,
sharing, Command, company-access and owner tests also passed.

Database coverage uses SQLite and the real PostgreSQL compatibility adapter
over a SQLite simulator. Native PostgreSQL concurrency, an actual browser
engine, real browser printing, the live Render service, new AI analysis, email
and Stripe were not exercised. Complete the staging workflow before production
use. A passing health check verifies installation, not the real user workflow.

For reproduction, extract `verification/Reproduction_Tests.zip` beside the
four application files in a separate local test directory. With the app's
dependencies plus pytest, httpx and beautifulsoup4 installed:

```bash
BC_TEST_APP=full_app.py python -m pytest -q test_daily_command.py test_blueprint_field.py test_sharing.py test_command.py test_invitations.py test_team_access.py test_browser_forms.py test_form_submission.py test_owner_console.py test_owner_team_access.py
python smoke_runtime.py --support-dir . --output-dir verification-output --render-proxy
```

Historical baseline comparison tests may skip when their original files are
unavailable. Test helpers use disposable local data and do not contact the live
service.
