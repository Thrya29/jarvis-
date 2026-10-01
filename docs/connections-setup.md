# Setting up "Sign in with Microsoft / Google"

JARVIS users connect their own email, calendar and files from **Connections** in the app.
They sign in on Microsoft's or Google's own page; JARVIS never sees their password, and
the access it receives is limited to the boxes they ticked.

Before users can do that, Microsoft and Google need to know about this copy of JARVIS.
That's a one-time, free **app registration**, done by whoever distributes JARVIS (you,
or a company's IT team). It produces an *app ID* (not a password), which is entered under
**Connections → Advanced** or in `config.toml`:

```toml
[connections]
microsoft_client_id = "00000000-0000-0000-0000-000000000000"
microsoft_tenant = "common"        # or your organisation's tenant ID to allow only it
google_client_id = "1234-abc.apps.googleusercontent.com"
google_client_secret = "GOCSPX-..." # Google's "desktop app" secret; not confidential
```

IMAP accounts (Zoho, company mail servers) need no registration. Users enter their server
and an app password directly.

## Microsoft (Outlook, Microsoft 365, OneDrive)

1. Go to the [Microsoft Entra admin center](https://entra.microsoft.com) (or Azure portal) →
   **App registrations** → **New registration**.
2. **Name:** `JARVIS`.
3. **Supported account types:** *Accounts in any organizational directory and personal
   Microsoft accounts* (matches `microsoft_tenant = "common"`). Choose *this organizational
   directory only* if JARVIS is just for your company, and set `microsoft_tenant` to your
   tenant ID.
4. **Redirect URI:** platform **Public client/native (mobile & desktop)**, value
   `http://localhost`. JARVIS listens on a random local port at sign-in, and Microsoft
   accepts any port for `localhost`.
5. Register, then copy the **Application (client) ID** into `microsoft_client_id`.
6. Optional but recommended for companies: **API permissions** → **Add a permission** →
   Microsoft Graph → *Delegated*, and add the ones you'll allow:

   | JARVIS option | Graph permission |
   |---|---|
   | (always) | `User.Read`, `offline_access` |
   | Read email | `Mail.Read` |
   | Write drafts | `Mail.ReadWrite` |
   | Send email | `Mail.ReadWrite`, `Mail.Send` |
   | Read calendar | `Calendars.Read` |
   | Create events | `Calendars.ReadWrite` |
   | Files | `Files.Read.All` |

   An admin can then click **Grant admin consent** so employees aren't blocked.

No client secret is needed: JARVIS is a desktop ("public") app and uses PKCE instead.

**Work accounts.** Many organisations only let users approve apps their IT team has
vetted. If a user sees *"Need admin approval"*, JARVIS shows: *"your organisation requires
an administrator to approve JARVIS…"*. The fix is step 6's admin consent, done by IT.

## Google (Gmail, Google Calendar, Google Drive)

1. In the [Google Cloud console](https://console.cloud.google.com), create a project
   (e.g. `JARVIS`).
2. **APIs & Services → Library**: enable **Gmail API**, **Google Calendar API** and
   **Google Drive API**.
3. **Google Auth Platform** (formerly *OAuth consent screen*):
   - **Audience:** *External* for any Google account, or *Internal* for your Google
     Workspace organisation only (no verification needed).
   - **Data access:** add the scopes you'll allow:
     `gmail.readonly`, `gmail.compose`, `calendar.readonly`, `calendar.events`,
     `drive.readonly` (plus `openid` and `email`).
   - While the app is in **Testing**, add each user's Gmail address under **Test users**
     (up to 100).
4. **Clients → Create client → Desktop app**. Copy the **client ID** and **client secret**
   into `google_client_id` and `google_client_secret`. Google documents that a desktop
   app's secret is not confidential, so it can live in the config file.

Things to know before inviting real users:

- **Testing mode signs users out weekly.** Google expires refresh tokens for External apps in
  *Testing* after 7 days, so users must reconnect each week until the app is published.
- **Publishing needs Google verification.** `gmail.readonly`, `gmail.compose` and
  `drive.readonly` are *restricted* scopes. Making the app available to everyone requires
  Google's verification and an annual third-party security assessment. *Internal*
  (Workspace) apps are exempt.

## What users see

1. **Connections** → tick what JARVIS may do (read email, write drafts, send, read
   calendar, create events, files) → **Sign in with Microsoft/Google**.
2. Their browser opens the provider's page, which lists exactly those permissions.
3. After approving, the tab says *Connected* and the account appears in JARVIS.
4. **Disconnect** deletes the tokens on the PC. For Google, it also revokes the grant at
   Google. Microsoft users can remove the app at
   [myapps.microsoft.com](https://myapps.microsoft.com) / account.live.com.

Tokens are stored encrypted with Windows DPAPI under the user's data folder
(`%LOCALAPPDATA%\Jarvis\Jarvis\connections\`). Only that Windows user, on that PC, can decrypt
them.
