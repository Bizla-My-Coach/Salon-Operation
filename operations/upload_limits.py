"""Bound workbook bodies before CSRF's multipart parsing, including chunked uploads."""
from django.core.files.uploadhandler import FileUploadHandler, StopUpload
from django.http import HttpResponse

from .sop_imports import MAX_UPLOAD
from .staff_imports import MAX_STAFF_UPLOAD
from .branch_imports import MAX_BRANCH_UPLOAD
from .daily_reports import MAX_DAILY_UPLOAD


class WorkbookUploadLimit(FileUploadHandler):
    max_bytes = MAX_UPLOAD

    def receive_data_chunk(self, raw_data, start):
        if start + len(raw_data) > self.max_bytes:
            raise StopUpload(connection_reset=True)
        return raw_data

    def file_complete(self, file_size):
        return None


class StaffCSVUploadLimit(WorkbookUploadLimit):
    max_bytes = MAX_STAFF_UPLOAD


class BranchCSVUploadLimit(WorkbookUploadLimit):
    max_bytes = MAX_BRANCH_UPLOAD


class DailyReportUploadLimit(WorkbookUploadLimit):
    max_bytes = MAX_DAILY_UPLOAD


class WorkbookUploadLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        limits = {
            '/admin-panel/import/workbook/': (MAX_UPLOAD, WorkbookUploadLimit, 'Workbook'),
            '/staff/import/': (MAX_STAFF_UPLOAD, StaffCSVUploadLimit, 'Staff CSV'),
            '/imports/branches/': (MAX_BRANCH_UPLOAD, BranchCSVUploadLimit, 'Branch CSV'),
            '/reports/daily/': (MAX_DAILY_UPLOAD, DailyReportUploadLimit, 'Daily report'),
        }
        if request.method == 'POST' and request.path in limits:
            maximum, handler, label = limits[request.path]
            try:
                length = int(request.META.get("CONTENT_LENGTH") or 0)
            except ValueError:
                return HttpResponse("Invalid upload length.", status=400)
            if length > maximum + 1024 * 1024:
                return HttpResponse(f'{label} upload is too large.', status=413)
            request.upload_handlers.insert(0, handler(request))
        return self.get_response(request)
