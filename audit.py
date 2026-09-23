from core.audit import LibraryAudit

from core.report import ReportGenerator


audit = LibraryAudit(
    "storage/library.db"
)

result = audit.full_audit()

report = ReportGenerator()

path = report.save(
    result
)

print(
    "报告生成:",
    path
)
