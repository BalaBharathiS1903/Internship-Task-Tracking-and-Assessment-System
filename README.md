# Internship Desk

Internship Desk is a Django workspace for intern onboarding, task tracking, attendance, and submission review. The coordinator assigns tasks and verifies completed work. Interns submit their own work. An intern designated as Team Lead can monitor the team's task status and send individual in-app messages.

## Run locally on Windows

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createsuperuser
.\.venv\Scripts\python.exe manage.py runserver
```

Open [Internship Desk](http://127.0.0.1:8000/) and sign in to set up internship batches, domains, and teams. Register each intern, then share the private activation link from their record. A superuser can add coordinator accounts to the `Coordinators` group in Django administration.

## Main workflows

1. **Onboard.** Enter approved intern records manually or download the Intern Master template from Upload Center. Enter College and Domain for each intern. Workbook intake assigns the least populated team in the batch and domain, creating the first team if needed. Complete the optional Setup sheet for new batches or domains. Use a batch ID prefix only when authorized to issue DIT IDs locally.
2. **Assign.** Set a start date and due date for each task. Assign tasks individually or use the task register workbook.
3. **Submit.** Interns enter work details and upload a document for their own assignment. The personal submission manifest supports multiple assignments and document attachments. The server derives the DIT ID from the authenticated account.
4. **Verify.** The coordinator checks the work details and uploaded document, then marks the task Completed or Needs revision. A revision includes comments and a new deadline. The intern sees the status in My reviews.
5. **Monitor.** Team Leads use the same intern account and their read-only Team workspace to view team task status. Messages go to the selected teammate's inbox. The coordinator manages attendance and exports team follow-ups, task registers, Intern Master, attendance, and review records.

Every workbook includes an example and version metadata. Fill its Data sheet and confirm the preview to commit changes. When a row fails, download its error report, correct the source workbook, and upload again.

## Settings and automatic email

Settings includes account details, password changes, coordinator user management, Team Lead designation, recent activity, and email delivery status. When a new intern record has an email address, the app generates a username and a temporary password and queues a welcome email. The intern must change the temporary password at first sign-in. Records without email can use the private activation link in Intern Master.

Coordinators can download user names, usernames, and valid onboarding passwords from User management. The password status column shows which accounts require activation or already have a password set.

Each newly assigned task queues a separate email to the assigned intern, including the task code, title, start date, due date, instructions, and assignment link. A completed review queues the decision for the intern. Workbook previews and task edits do not send mail.

On Render, set `DIT_EMAIL_FROM` to a verified Resend sender such as `DefineInfoTech Internship Desk <internships@your-verified-domain.example>` and set `DIT_RESEND_API_KEY` in the service's Environment page. `DIT_PUBLIC_URL` overrides the portal URL for email links when using a custom domain. Render's free web service blocks standard SMTP ports, so the default transport uses Resend over HTTPS.

After configuring delivery, open **Settings → Email notifications** to send any ready emails. Locally, `python manage.py deliver_notifications` also drains ready entries. Delivery failures remain in the queue for retry. For a local SMTP deployment, use `DIT_EMAIL_TRANSPORT=django` and configure the Django email backend separately.

## Render demo

The Blueprint in `render.yaml` runs one free web service in Singapore with SQLite. The database starts empty, with the `DITcoordinator` account provisioned from the `DJANGO_COORDINATOR_PASSWORD` environment variable.

To provision a Django admin account on Render, configure `DJANGO_SUPERUSER_USERNAME` and `DJANGO_SUPERUSER_PASSWORD` in the service environment. Startup creates the account once per database. Django admin is at `/admin/`.

Free Render services use an ephemeral filesystem. Database records and uploaded files reset on restart, redeploy, or idle spin-down. Use synthetic records for the hosted demo.

## PostgreSQL configuration

Set `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, and optionally `POSTGRES_PORT` before running migrations. Set `DJANGO_DEBUG=0`, a strong `DJANGO_SECRET_KEY`, and `DJANGO_ALLOWED_HOSTS` for a production environment. Serve via an HTTPS WSGI deployment and run `collectstatic`. Keep the database and private upload directory on backed-up storage. Run `python manage.py prepare_followups` daily with the host scheduler to refresh the date's follow-up list.

## Verification

```powershell
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
```

See [the implementation plan](IMPLEMENTATION_PLAN.md) for the design and acceptance criteria. The Django application code lives in `tracking/`, with the responsibilities split across access checks, models, workflow services, Excel intake, reporting, and route handlers.
