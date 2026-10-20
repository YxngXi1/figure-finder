# Share the Slides beta with supervisors

This guide hosts the existing Python app on a **Free Render web service**.
Users enter the shared beta password, authorize their own Google account, select
a presentation, and run the agent. Users do not need Python or API keys.
The host provides an HTTPS `onrender.com` address; a purchased domain is optional.

## 1. Publish the source to your GitHub repository

Commit and push the current app source, including `figurefinder/beta*`,
`requirements-beta.txt`, `gunicorn_beta.py`, and `render-beta.yaml`.
Keep the repository private if you prefer; grant Render access to that repository.
Do not upload `.env`, `.secrets`, downloaded images or generated run folders.
The existing `.gitignore` excludes these. Review the files staged for commit.

Your local `.env` is still used for local runs. Hosted settings are entered
separately in Render's Environment settings; Render does not read your laptop's
`.env`.

## 2. Create the free web service

In Render, choose **New → Web Service**, connect the repository, then use:

| Setting | Value |
| --- | --- |
| Language | Python 3 |
| Instance type | **Free** |
| Build command | `pip install -r requirements-beta.txt` |
| Start command | `gunicorn --config gunicorn_beta.py 'figurefinder.beta:create_app()'` |
| Health check | `/healthz` |
| Auto deploy | Off during supervisor testing |

Alternatively, create a Blueprint using the **`render-beta.yaml`** path.
The old `render.yaml` starts the earlier single-image search interface.
Do not add a paid worker, database or disk. One web process runs one image job
at a time. Manual deployment avoids interrupting tests on every source push.

Render's Free plan has bandwidth/build allowances. Without a payment method,
Render suspends usage or builds at the relevant limits instead of billing
overages. If your existing workspace has a payment method, review its billing
and spend settings before deployment; a Free instance does not guarantee zero
workspace charges.

## 3. Set the hosted environment values

Copy these values from the matching lines in your local `.env` directly into
Render's Environment screen (not into the repository):

- `OPENAI_API_KEY`
- `BRAVE_API_KEY`
- `FF_BETA_PASSWORD` — the shared password already chosen for this beta.
- `FF_GOOGLE_WEB_CLIENT_ID`
- `FF_GOOGLE_PICKER_KEY`
- `FF_GOOGLE_PROJECT_NUMBER`

Also set:

- `FF_BETA_SESSION_SECRET`: use a fresh random secret of at least 32 characters.
  The Blueprint generates one automatically; manual setup requires you to
  generate one, for example with `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.
- `FF_BETA_SECURE_COOKIES=1` for the hosted HTTPS address. Keep the local setting
  at `0` when using HTTP on localhost.
- `FF_SUPPORT_EMAIL`: your contact email, shown on the privacy/help page.

Optional limits are already supplied in the Blueprint: 15 slides per deck, up
to 3 figures per slide, 10 runs per 24-hour window, and 20 minutes per run.
The run counter is process-local and resets when the service restarts. It is
not a hard billing cap; API usage still belongs to your existing accounts.

## 4. Authorize the hosted address in Google

After Render assigns the service address, replace `YOUR-SERVICE` below:

1. In **Google Auth Platform → Clients**, edit the existing Web application
   client. Add `https://YOUR-SERVICE.onrender.com` to Authorized JavaScript
   origins. Keep the localhost entries for local testing. No redirect URI is
   needed for this app's browser token flow.
2. Edit the Picker API key. Add `https://YOUR-SERVICE.onrender.com/*` to its
   Website restrictions. Keep `https://docs.google.com/*` and your local entries.
   Its API restrictions should allow Google Picker API and Google Drive API.
3. In **Google Auth Platform → Audience**, keep the app in Testing and add each
   supervisor's exact Google sign-in email to Test users. They must use those
   accounts to authorize Google access. School/work administrators may need to
   approve the app for their organization.
4. Keep the `https://www.googleapis.com/auth/drive.file` scope and ensure Slides,
   Drive and Picker APIs are enabled in this same project.

The shared password unlocks the website. Google consent and file permissions
control presentation access separately; knowing the password does not expose
your Google account. Website access itself is by shared password, not an
individual supervisor allowlist.

## 5. Test before sharing

1. Deploy, open the assigned HTTPS address, and confirm the password gate.
2. Use a private/incognito browser window to exercise a fresh session.
3. Sign in as a Google test user and choose a **copy** of a small presentation.
4. Start with **1 per slide**. Confirm a content slide receives an appropriate
   image, caption and citation when space permits, and title/transition slides
   can be skipped.
5. Expand slide results and each figure's candidate debug panel.
6. Have a second authorized tester use another browser/device. Confirm they see
   only their own runs and can select only presentations available to their account.

The service sleeps after 15 minutes without inbound traffic and can take about
a minute to wake up. Keep the testing page open during a run. A restart can
interrupt a run and clear sessions/history; edits already saved to Google Slides
remain. Check the deck before retrying. Start with a small deck while assessing
free-host memory and runtime limits.

## What to send supervisors

Share the actual deployed URL and invitation password privately, plus:

> Sign in using the Google email registered for testing. Choose a copy of a deck
> you can edit, begin with 1 figure per slide, and keep the page open while it
> runs. Please report slide numbers where the figure choice, caption, citation,
> alignment or skip decision seems wrong. The app edits the chosen deck directly.

No URL is live merely because these files exist. Deployment and the hosted
Google-origin setup must be completed first.

Sources: [Render Flask deployment](https://render.com/docs/deploy-flask),
[Render free limits](https://render.com/docs/free),
[Google test users](https://developers.google.com/workspace/guides/configure-oauth-consent),
[Google Picker setup](https://developers.google.com/workspace/drive/picker/guides/web-picker-sample).
