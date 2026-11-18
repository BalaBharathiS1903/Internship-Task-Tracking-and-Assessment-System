from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

from defusedxml.common import DefusedXmlException
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist, ValidationError
from django.db import IntegrityError, transaction
from django.http import FileResponse
from django.utils import timezone
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.workbook.defined_name import DefinedName

from . import models as m, services as s
from .access import own_assignments
from .reporting import rows

SCHEMAS = {
    "roster": ("Intern master", ("reference", "name", "email", "cohort", "dit_id", "mode", "domain", "preferred_domain", "team")),
    "tasks": ("Task register", ("reference", "task_code", "title", "dit_id", "cohort", "team", "rubric", "assigned_date", "due_date", "instructions")),
    "submissions": ("Submission manifest", ("assignment", "artifact", "link", "note")),
    "attendance": ("Attendance register", ("session", "dit_id", "status", "reason")),
    "criteria": ("Rubric criteria", ("rubric", "code", "name", "description", "weight", "minimum", "maximum", "step")),
    "scores": ("Score register", ("assignment", "version", "criterion", "score", "comment", "feedback")),
}
EXAMPLES = {
    "roster": ("STU-100", "Aparna Rao", "aparna@example.invalid", "C26", "", "manual", "WEB", "", "WEB-A"),
    "tasks": ("WEB-100", "WEB-01", "Create a prototype", "DIT-C26-0001", "C26", "WEB-A", "Project v1", "2026-11-10", "2026-11-17", ""),
    "submissions": ("WEB-100", "prototype.pdf", "", "First submission"),
    "attendance": ("SESSION-01", "DIT-C26-0001", "present", ""),
    "criteria": ("Project v1", "QUALITY", "Quality", "Working result", "100", "0", "10", "1"),
    "scores": ("WEB-100", "1", "QUALITY", "8", "Good coverage", ""),
}
MAX_ROWS = 500
VERSION = "1"


def style(sheet):
    sheet.freeze_panes = "A2"
    for column in sheet.columns:
        letter = column[0].column_letter
        sheet.column_dimensions[letter].width = min(55, max(16, max(len(str(cell.value or "")) for cell in column) + 3))
    for cell in sheet[1]:
        cell.fill = PatternFill("solid", fgColor="152D40")
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(vertical="center")
    sheet.row_dimensions[1].height = 24
    if sheet.max_row > 1:
        sheet.auto_filter.ref = sheet.dimensions


def add_rows(sheet, content):
    for row in content:
        sheet.append(row)
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = "s"
                cell.number_format = "@"


def workbook_response(workbook, filename):
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return FileResponse(output, as_attachment=True, filename=filename)


def template(kind, user):
    label, headers = SCHEMAS[kind]
    book = Workbook()
    sheet = book.active
    sheet.title = "Data"
    add_rows(sheet, [headers])
    help_sheet = book.create_sheet("Instructions")
    info = [
        ("Workflow", label), ("Version", VERSION),
        ("Instructions", "Fill Data only. Retain the column headings. Use YYYY-MM-DD dates and text IDs."),
        ("Batch limit", str(MAX_ROWS)),
    ]
    if kind == "submissions":
        info.append(("Artifact", "Enter the exact filename of an attached file, or use an HTTPS link. Remove unused rows."))
        eligible = [item for item in rows(own_assignments(user, m.Assignment.objects.all())) if item["can_submit"]]
        add_rows(sheet, [(item["assignment"].reference, "", "", "") for item in eligible[:MAX_ROWS]])
    add_rows(help_sheet, info)
    examples = book.create_sheet("Example")
    add_rows(examples, [headers, EXAMPLES[kind]])
    setup = book.create_sheet("Lists")
    setup.sheet_state = "hidden"
    choices = {
        "cohort": m.Cohort.objects.values_list("code", flat=True),
        "domain": m.Domain.objects.values_list("code", flat=True),
        "preferred_domain": m.Domain.objects.values_list("code", flat=True),
        "team": m.Team.objects.order_by("code").values_list("code", flat=True).distinct(),
        "mode": ["manual", "auto"],
        "status": m.Attendance.Status.values,
    }
    for column, header in enumerate(headers, 1):
        if header not in choices:
            continue
        letter = get_column_letter(column)
        for row, choice in enumerate(choices[header], 1):
            setup.cell(row, column, choice).data_type = "s"
        if setup.cell(1, column).value:
            size = len(choices[header])
            list_name = f"Desk_{header}"
            book.defined_names.add(DefinedName(list_name, attr_text=f"'Lists'!${letter}$1:${letter}${size}"))
            validation = DataValidation(type="list", formula1=f"={list_name}")
            validation.showErrorMessage = True
            validation.error = "Choose an available value from the list."
            sheet.add_data_validation(validation)
            validation.add(f"{letter}2:{letter}{MAX_ROWS + 1}")
    for page in (sheet, help_sheet, examples):
        style(page)
    if kind != "submissions":
        for column in sheet.columns:
            if column[0].value in ("reference", "dit_id", "code", "team"):
                sheet.column_dimensions[column[0].column_letter].number_format = "@"
    return workbook_response(book, f"{kind}-template.xlsx")


def parse(upload, kind):
    if not upload.name.lower().endswith(".xlsx") or upload.size > 5 * 1024 * 1024:
        raise ValidationError("Use a prebuilt .xlsx workbook no larger than 5 MB.")
    try:
        with ZipFile(upload) as archive:
            if len(archive.infolist()) > 100 or sum(entry.file_size for entry in archive.infolist()) > 20 * 1024 * 1024:
                raise ValidationError("The workbook expands beyond the supported size.")
        upload.seek(0)
        book = load_workbook(upload, read_only=True, keep_links=False)
        try:
            headers = SCHEMAS[kind][1]
            metadata = book["Instructions"]
            if str(metadata["B2"].value) != VERSION or metadata["B1"].value != SCHEMAS[kind][0]:
                raise ValidationError("Download the current template for this workflow.")
            data = book["Data"]
            data.reset_dimensions()
            sheet_rows = data.iter_rows(max_col=len(headers) + 1)
            actual = next(sheet_rows, ())
            if tuple(cell.value for cell in actual[:len(headers)]) != headers or actual[-1].value is not None:
                raise ValidationError("The Data sheet headers must match the selected template.")
            result = []
            for number, sheet_row in enumerate(sheet_rows, 2):
                if number > MAX_ROWS + 1:
                    raise ValidationError(f"Split the import into batches of {MAX_ROWS} rows or fewer.")
                if not any(cell.value is not None for cell in sheet_row):
                    continue
                if sheet_row[-1].value is not None or any(cell.data_type in ("f", "e") for cell in sheet_row):
                    raise ValidationError(f"Row {number}: remove formulas, errors, and extra columns.")
                if any(name == "dit_id" and cell.value is not None and not isinstance(cell.value, str)
                       for name, cell in zip(headers, sheet_row)):
                    raise ValidationError(f"Row {number}: enter the DIT ID as text to preserve leading zeros.")
                values = {name: str(cell.value).strip()[:4001] if cell.value is not None else ""
                          for name, cell in zip(headers, sheet_row)}
                if kind == "submissions" and not (values["artifact"] or values["link"] or values["note"]):
                    continue
                result.append({"number": number, **values})
            if not result:
                raise ValidationError("Add at least one data row to the workbook.")
            return result
        finally:
            book.close()
    except (BadZipFile, InvalidFileException, ParseError, DefusedXmlException, KeyError, OSError, ValueError) as exc:
        raise ValidationError("The workbook could not be read. Download a fresh template.") from exc


def date_value(value):
    return date.fromisoformat(value.split(" ")[0])


def rubric_by_label(value):
    name, marker, version = value.rpartition(" v")
    if not marker:
        raise ValidationError("Use a rubric label such as Project v1.")
    return m.Rubric.objects.get(name__iexact=name, version=int(version))


def find_domain(value):
    return m.Domain.objects.get(code__iexact=value) if value else None


def handle_row(batch, row, artifacts, dry_run):
    actor, kind = batch.actor, batch.kind
    if kind == "roster":
        cohort = m.Cohort.objects.get(code__iexact=row["cohort"])
        team = m.Team.objects.get(cohort=cohort, code__iexact=row["team"]) if row["team"] else None
        mode = row["mode"].lower() or "manual"
        if mode not in ("manual", "auto"):
            raise ValidationError("Domain allocation mode must be manual or auto.")
        enrollment = s.register_intern(
            actor, reference=row["reference"], name=row["name"], email=row["email"], dit_id=row["dit_id"],
            cohort=cohort, mode=mode, domain=find_domain(row["domain"]), preferred_domain=find_domain(row["preferred_domain"]),
            team=team, notify=not dry_run,
        )
        return f"{enrollment.intern.dit_id} · {enrollment.domain}"
    if kind == "tasks":
        enrollment = m.Enrollment.objects.get(intern__dit_key=row["dit_id"].upper(), cohort__code__iexact=row["cohort"])
        if not enrollment.team_id or enrollment.team.code.upper() != row["team"].upper():
            raise ValidationError("The team code must match the intern's current allocation.")
        s.assign_task(
            actor, enrollment=enrollment, reference=row["reference"].upper(), task_code=row["task_code"],
            title=row["title"], instructions=row["instructions"], rubric=rubric_by_label(row["rubric"]),
            assigned_date=date_value(row["assigned_date"]), due_date=date_value(row["due_date"]), notify=not dry_run,
        )
    if kind == "submissions":
        assignment = own_assignments(actor, m.Assignment.objects).get(reference__iexact=row["assignment"])
        artifact = artifacts.get(row["artifact"].casefold()) if row["artifact"] else None
        if row["artifact"] and not artifact:
            raise ValidationError("Attach the exact project filename listed in this row.")
        s.submit(actor, assignment.pk, link=row["link"], note=row["note"], staged=artifact, dry_run=dry_run)
    if kind == "attendance":
        session = m.AttendanceSession.objects.get(reference__iexact=row["session"])
        enrollment = m.Enrollment.objects.get(intern__dit_key=row["dit_id"].upper(), cohort=session.team.cohort)
        s.mark_attendance(actor, session, enrollment, row["status"].lower(), row["reason"])
    if kind == "criteria":
        s.add_criterion(actor, rubric_by_label(row["rubric"]), code=row["code"].upper(), name=row["name"],
                        description=row["description"], weight=decimal_value(row["weight"]),
                        minimum=decimal_value(row["minimum"]), maximum=decimal_value(row["maximum"]),
                        step=decimal_value(row["step"]))
    if kind == "scores":
        submission = m.Submission.objects.get(assignment__reference__iexact=row["assignment"], version=int(row["version"]))
        criterion = submission.assignment.rubric.criteria.get(code__iexact=row["criterion"])
        s.assess(actor, submission, {criterion.pk: (decimal_value(row["score"]), row["comment"], row["feedback"])})
    return "Ready"


def decimal_value(text):
    value = Decimal(text)
    if not value.is_finite() or abs(value) >= 1000000 or value.as_tuple().exponent < -2:
        raise ValidationError("Use a finite decimal with up to two decimal places.")
    return value


KEYS = {
    "roster": ("reference",),
    "tasks": ("reference",),
    "submissions": ("assignment",),
    "attendance": ("session", "dit_id"),
    "criteria": ("rubric", "code"),
    "scores": ("assignment", "version", "criterion"),
}


@transaction.atomic
def apply_batch(batch, dry_run=True):
    batch = m.ImportBatch.objects.select_for_update().get(pk=batch.pk)
    if batch.committed_at:
        return []
    files = {item.name.casefold(): item for item in batch.artifacts.all()}
    output, seen = [], set()
    for row in batch.rows:
        key = tuple(row[field].lower() for field in KEYS[batch.kind])
        error = ""
        detail = ""
        try:
            if any(not part for part in key):
                raise ValidationError("Fill all required reference columns.")
            if key in seen:
                raise ValidationError("Duplicate row reference in this upload.")
            seen.add(key)
            with transaction.atomic():
                detail = handle_row(batch, row, files, dry_run)
        except ValidationError as exc:
            error = " ".join(exc.messages)
        except (ObjectDoesNotExist, MultipleObjectsReturned):
            error = "An identifier does not match a record available for this workflow."
        except (InvalidOperation, ValueError, OverflowError):
            error = "Use valid dates, versions, and numeric scores."
        except IntegrityError:
            error = "A unique identifier is already in use or a record constraint failed."
        output.append({"number": row["number"], "reference": " · ".join(key), "error": error, "detail": detail})
    if dry_run or any(item["error"] for item in output):
        transaction.set_rollback(True)
    else:
        batch.committed_at = timezone.now()
        batch.save(update_fields=["committed_at"])
        s.audit(batch.actor, "import.committed", batch.kind, f"{len(output)} rows")
    return output


def error_report(result):
    book = Workbook()
    sheet = book.active
    sheet.title = "Row Errors"
    add_rows(sheet, [("Row", "Reference", "Error")] + [
        (item["number"], item["reference"], item["error"]) for item in result if item["error"]
    ])
    style(sheet)
    return workbook_response(book, "import-errors.xlsx")
