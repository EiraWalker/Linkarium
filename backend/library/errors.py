class ApiError(Exception):
    def __init__(self, code, message, status=400, details=None, field_errors=None):
        self.code, self.message, self.status = code, message, status
        self.details, self.field_errors = details, field_errors
        super().__init__(message)
