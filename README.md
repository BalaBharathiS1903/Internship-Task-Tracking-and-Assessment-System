# Internship Desk

Internship Desk is a Django workspace for intern onboarding, task tracking, attendance, and rubric-based assessment. The coordinator assigns tasks and publishes results. Interns submit their own work. An intern designated as Team Lead can monitor the team's task status and send individual in-app messages.

## Run locally on Windows

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createsuperuser
.\.venv\Scripts\python.exe manage.py runserver
```

Open [Internship Desk](http://127.0.0.1:8000/) and sign in to set up internship batches, domains, teams, and scoring guides. Register each intern, then share the private activation link from their record. A superuser can add coordinator accounts to the `Coordinators` group in Django administration.

## Main workflows

1. **Onboard.** Enter approved intern records manually or download the prebuilt roster template from Upload Center. Use manual domain selection or automated matching from a preference and the selected team's domain. Use a batch ID prefix only when authorized to issue DIT IDs locally.
2. **Assign.** Publish a scoring guide with weights totaling 100%. Assign tasks individually or use the task register workbook.
3. **Submit.** Interns sign in and provide an HTTPS link or a project file. The personal submission manifest supports multiple assignments and file attachments. The server derives the DIT ID from the authenticated account.
4. **Assess.** The coordinator reviews evidence, enters scores or imports the score workbook, checks criterion feedback, and publishes Passed or Needs revision. A revision opens another submission cycle with its own deadline.
5. **Monitor.** Team Leads use the same intern account and their read-only Team workspace. Messages go to the selected teammate's inbox. The coordinator manages attendance and exports team follow-ups, task registers, roster, attendance, and released results.

Every workbook includes an example and version metadata. Fill its Data sheet and confirm the preview to commit changes. When a row fails, download its error report, correct the source workbook, and upload again.

## Render demo

The Blueprint in `render.yaml` runs one free web service in Singapore with SQLite. The database starts empty, with the `DITcoordinator` account provisioned from the `DJANGO_COORDINATOR_PASSWORD` environment variable.

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
